#!/usr/bin/env python3
from flask import Flask, request, jsonify
import subprocess
import json
import os
import re
import tempfile
from datetime import datetime

import requests

from pcg_rules import classify, build_ledger_entry

app = Flask(__name__)

ANALYZOR_URL = os.environ.get('ANALYZOR_URL', 'http://localhost:8000')
SUBSCRIPTIONS_URL = os.environ.get('SUBSCRIPTIONS_URL', 'http://localhost:8082')
SERVICE_API_KEY = os.environ.get('SERVICE_API_KEY', '')
# Rotation de cle sans coupure : SERVICE_API_KEYS (liste separee par des virgules) est
# acceptee EN PLUS de SERVICE_API_KEY. Si SERVICE_API_KEYS est absent, le comportement
# est strictement identique a avant (seule SERVICE_API_KEY est acceptee).
_ACCEPTED_SERVICE_KEYS = frozenset({SERVICE_API_KEY} | {
    k.strip() for k in os.environ.get('SERVICE_API_KEYS', '').split(',') if k.strip()
})


def _resolve_role(org_id, email):
    """Role de l'utilisateur (email SSO) sur l'org, via subscriptions_api. None si non-membre/erreur."""
    if not email:
        return None, None
    try:
        r = requests.get(f'{SUBSCRIPTIONS_URL}/api/auth/membership',
                         params={'orgId': org_id, 'email': email},
                         headers={'X-Service-Key': SERVICE_API_KEY}, timeout=5)
        d = r.json()
        if d.get('isMember'):
            return d.get('role'), d.get('uid')
        return None, None
    except Exception:
        return None, None


def _authorize_write(org_id, data):
    """Controle d'acces backend pour toute ecriture. Non contournable :
    - l'appelant doit presenter la service-key (seuls les backends de confiance l'ont) ;
    - l'email SSO doit etre membre editor/owner (viewer = lecture seule).
    Retourne (email, None) si OK, sinon (None, (reponse_erreur, status))."""
    if request.headers.get('X-Service-Key') not in _ACCEPTED_SERVICE_KEYS:
        return None, (jsonify({'success': False, 'error': 'appelant non autorise (service-key)'}), 401)
    email = (data.get('userEmail') or '').strip().lower()
    if not email:
        return None, (jsonify({'success': False, 'error': 'userEmail manquant'}), 400)
    role, uid = _resolve_role(org_id, email)
    if role is None:
        return None, (jsonify({'success': False, 'errorCode': 'not_member', 'error': 'non membre de cette organisation'}), 403)
    if role not in ('editor', 'owner'):
        return None, (jsonify({'success': False, 'errorCode': 'read_only', 'error': 'droit insuffisant : viewer (lecture seule)', 'role': role}), 403)
    return uid, None


def log_to_journal(org_id, actor, summary, details=None):
    """Journal technique automatique (demandé par Stéphane 2026-07-18) : chaque action
    réelle du cœur comptable se logue toute seule, sans intervention manuelle. Fail-open
    strict — une panne du journal ne doit jamais faire échouer une vraie écriture comptable."""
    try:
        requests.post(
            f'{ANALYZOR_URL}/api/journal/log',
            json={'orgId': org_id, 'actor': actor, 'summary': summary, 'details': details or []},
            timeout=3,
        )
    except requests.RequestException:
        pass

JOURNAL_PATH = '/home/ubuntu/ledger_api/journal.ledger'

# S'assurer que le fichier journal de base existe (même vide)
if not os.path.exists(JOURNAL_PATH):
    with open(JOURNAL_PATH, 'w') as f:
        f.write('')

# ============================================================
# MULTI-ORGANISATION (BYOS v0 : un fichier par org sur disque,
# derrière une interface qu'on pourra brancher sur Drive plus tard)
# ============================================================

ORGS_DIR = '/home/ubuntu/ledger_api/orgs'
ORG_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')

QUERY_COMMANDS = {'balance', 'bal', 'register', 'reg', 'equity', 'print', 'accounts', 'csv'}


class NeedsBootstrapError(Exception):
    """Le Drive de l'org existe mais n'a pas encore de fichier journal.ledger — le compte de
    service ne peut jamais le CRÉER lui-même (storageQuotaExceeded). L'appelant (Apps Script,
    identité réelle) doit créer le placeholder puis réessayer — voir
    ConnectorIdentity.js::identityEnsureJournalPlaceholder et _callLedger (retry générique)."""
    def __init__(self, folder_id):
        self.folder_id = folder_id
        super().__init__('needs_bootstrap')


def org_journal_path(org_id, create=False):
    """Chemin LOCAL, TEMPORAIRE, du journal — jamais la source de vérité (2026-08-10, retour
    de Stéphane : "le journal doit être dans le storage de l'orga, pas sur le VPS"). Si l'org a
    un dossier Drive, son contenu est resynchronisé ICI, à chaque appel, depuis le fichier
    journal.ledger de CE Drive — jamais une copie locale qu'on se contente de réutiliser. Repli
    sur le fichier local pur pour les orgs sans dossier Drive (compat, ex. démos internes)."""
    if not ORG_ID_RE.match(org_id or ''):
        raise ValueError('orgId invalide')

    org_dir = os.path.join(ORGS_DIR, org_id)
    path = os.path.join(org_dir, 'journal.ledger')

    try:
        resp = requests.get(f'{ANALYZOR_URL}/api/ownstorage/journal', params={'orgId': org_id}, timeout=10)
        data = resp.json()
    except requests.RequestException:
        data = {'success': False, 'errorCode': 'analyzor_unreachable'}

    if data.get('success'):
        os.makedirs(org_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data.get('content') or '')
        return path

    if data.get('errorCode') == 'needs_bootstrap':
        raise NeedsBootstrapError(data.get('folderId'))

    # unknown_org (pas de dossier Drive) ou analyzor injoignable : repli local pur, comportement
    # historique inchangé — ne bloque jamais une org qui n'a jamais eu de présence Drive.
    if create and not os.path.exists(path):
        os.makedirs(org_dir, exist_ok=True)
        with open(path, 'w') as f:
            f.write('; Journal créé automatiquement pour org=' + org_id + '\n')

    return path


def push_journal_to_drive(org_id, path):
    """Pousse le contenu local vers le Drive de l'org après une écriture — no-op silencieux si
    l'org n'a pas de dossier Drive (repli local pur) ou si le placeholder n'existe pas encore
    (l'appelant Apps Script est responsable du bootstrap avant d'écrire, voir _callLedger)."""
    try:
        with open(path, encoding='utf-8') as f:
            content = f.read()
        requests.post(
            f'{ANALYZOR_URL}/api/ownstorage/journal',
            json={'orgId': org_id, 'content': content},
            timeout=10,
        )
    except (requests.RequestException, OSError):
        pass


@app.route('/api/ledger/convert', methods=['POST'])
def convert_to_ledger():
    csv_path = None
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400

        csv_content = data.get('data', '')
        options = data.get('options', {})
        currency = options.get('currency', 'EUR')

        if not csv_content:
            return jsonify({'success': False, 'error': 'No CSV data provided'}), 400

        # Créer un fichier temporaire pour le CSV
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            csv_path = f.name

        # Convertir avec ledger (nécessite -f pour pointer vers un journal existant)
        try:
            result = subprocess.run(
                ['ledger', '-f', JOURNAL_PATH, 'convert', csv_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            journal = result.stdout
            error = result.stderr
            ledger_success = (result.returncode == 0)
        except subprocess.TimeoutExpired:
            journal = ''
            error = 'Timeout : la conversion a pris plus de 30 secondes'
            ledger_success = False
        except Exception as e:
            journal = ''
            error = str(e)
            ledger_success = False

        return jsonify({
            'success': ledger_success,
            'journal': journal,
            'error': error,
            'stats': {
                'lines': len(journal.split('\n')) if journal else 0,
                'currency': currency
            }
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

    finally:
        if csv_path and os.path.exists(csv_path):
            os.unlink(csv_path)


@app.route('/api/ledger/entry', methods=['POST'])
def add_entry():
    """Ajoute une écriture classée (partie double) au journal d'une organisation.

    Body: {orgId, libelle, montant, sens: "depense"|"recette", date?: "YYYY/MM/DD"}
    Crée le journal de l'org s'il n'existe pas encore (bootstrap BYOS).
    """
    try:
        data = request.get_json() or {}

        org_id = data.get('orgId', '')
        _actor, _err = _authorize_write(org_id, data)
        if _err:
            return _err
        libelle = (data.get('libelle') or '').strip()
        montant = data.get('montant')
        sens = data.get('sens') or 'depense'
        date = data.get('date') or datetime.now().strftime('%Y/%m/%d')

        if not libelle:
            return jsonify({'success': False, 'error': 'libelle manquant'}), 400
        if montant is None:
            return jsonify({'success': False, 'error': 'montant manquant'}), 400
        try:
            montant = float(str(montant).replace(',', '.'))
        except ValueError:
            return jsonify({'success': False, 'error': 'montant invalide'}), 400
        if sens not in ('depense', 'recette'):
            return jsonify({'success': False, 'error': 'sens doit être depense ou recette'}), 400

        try:
            path = org_journal_path(org_id, create=True)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        compte_info = classify(libelle, sens, org_id)
        sens_rule = 'debit' if sens == 'depense' else 'credit'
        contrepartie_rules = _load_contrepartie_rules(org_id)
        contrepartie = _get_contrepartie(compte_info['compte'], sens_rule, org_id) if contrepartie_rules else None
        entry_text = build_ledger_entry(date, libelle, montant, sens, compte_info, contrepartie=contrepartie, structory_user=_actor)

        with open(path, 'a') as f:
            f.write('\n' + entry_text + '\n')
        push_journal_to_drive(org_id, path)

        # Vérification d'équilibre via le vrai moteur ledger-cli
        balance_check = subprocess.run(
            ['ledger', '-f', path, 'balance', '--no-total'],
            capture_output=True, text=True, timeout=10
        )

        log_to_journal(
            org_id, 'ledger_api',
            f'Écriture ajoutée : {montant}€ — {libelle}',
            [f'Compte {compte_info["compte"]} ({compte_info["nom"]}), confiance {round(compte_info["confidence"] * 100)}%',
             f'Sens : {sens}, date : {date}'],
        )

        return jsonify({
            'success': True,
            'entry': entry_text,
            'compte': compte_info['compte'],
            'compteNom': compte_info['nom'],
            'confidence': compte_info['confidence'],
            'balanceCheck': balance_check.stdout
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ledger/import', methods=['POST'])
def import_entries():
    """Importe des écritures déjà résolues (comptes explicites sur chaque jambe,
    déterminés en amont par un autre outil - ex. Analyzor/journal_engine après
    réconciliation d'un classeur) dans le journal d'une organisation.

    Contrairement à /api/ledger/entry, aucune classification par mot-clé n'a
    lieu ici : le cœur comptable exécute ce qu'on lui donne, il ne décide de
    rien (le Connector n'a pas de logique métier - la décision des comptes a
    déjà été prise par l'appelant, ex. une Rule PCG).

    Body: {orgId, entries: [{date: "YYYY/MM/DD"|"YYYY-MM-DD", libelle,
           legs: [{compte, label?, amount}, ...]}], source?: str,
           mode?: "append"|"replace" (défaut "append")}
    `replace` sert à resynchroniser tout un groupe source (ex. relance de
    /sheettojournal) sans dupliquer les écritures déjà importées la fois
    d'avant - le journal existant est remplacé, pas complété.
    Chaque entrée doit avoir >= 2 jambes dont la somme des montants est nulle
    (tolérance 0.01). Le journal existant est sauvegardé avant toute écriture.
    """
    try:
        data = request.get_json() or {}

        org_id = data.get('orgId', '')
        _actor, _err = _authorize_write(org_id, data)
        if _err:
            return _err
        entries = data.get('entries')
        source = data.get('source') or ''
        mode = data.get('mode') or 'append'

        if mode not in ('append', 'replace'):
            return jsonify({'success': False, 'error': 'mode doit être append ou replace'}), 400

        if not isinstance(entries, list) or not entries:
            return jsonify({'success': False, 'error': 'entries manquant ou vide'}), 400

        blocks = []
        for i, entry in enumerate(entries):
            date = (entry.get('date') or '').replace('-', '/')
            libelle = (entry.get('libelle') or '').strip()
            legs = entry.get('legs') or []

            if not date or not libelle:
                return jsonify({'success': False, 'error': f'entries[{i}] : date/libelle manquant'}), 400
            if len(legs) < 2:
                return jsonify({'success': False, 'error': f'entries[{i}] : au moins 2 jambes requises'}), 400

            leg_lines = []
            total = 0.0
            for leg in legs:
                compte = str(leg.get('compte') or '').strip()
                amount = leg.get('amount')
                if not compte or amount is None:
                    return jsonify({'success': False, 'error': f'entries[{i}] : jambe invalide (compte/amount)'}), 400
                amount = float(amount)
                total += amount
                label = leg.get('label') or compte
                account_part = f"{compte}:{label}" if label != compte else compte
                leg_lines.append(f"    {account_part:<45}{amount:>10.2f} EUR")

            if abs(total) > 0.01:
                return jsonify({'success': False, 'error': f'entries[{i}] : jambes non équilibrées (somme={total:.2f})'}), 400

            blocks.append(f"{date} * {libelle}\n    # structory_user: {_actor}\n" + "\n".join(leg_lines))

        try:
            path = org_journal_path(org_id, create=True)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if os.path.exists(path):
            backup_path = path + f'.bak-{datetime.now().strftime("%Y%m%d%H%M%S")}'
            with open(path) as f_in, open(backup_path, 'w') as f_out:
                f_out.write(f_in.read())

        header = f'; Import {source} du {datetime.now().isoformat()} ({len(blocks)} écritures, mode={mode})\n'
        write_mode = 'w' if mode == 'replace' else 'a'
        with open(path, write_mode) as f:
            f.write('\n' + header + '\n\n'.join(blocks) + '\n')
        push_journal_to_drive(org_id, path)

        balance_check = subprocess.run(
            ['ledger', '-f', path, 'balance', '--no-total'],
            capture_output=True, text=True, timeout=15
        )

        log_to_journal(
            org_id, 'ledger_api',
            f'Import {mode} de {len(blocks)} écritures ({source or "source non précisée"})',
            [f'Équilibre vérifié via ledger-cli : {"OK" if balance_check.returncode == 0 else "ERREUR - " + balance_check.stderr[:200]}']
        )

        return jsonify({
            'success': True,
            'nImported': len(blocks),
            'balanceCheck': balance_check.stdout,
            'balanceError': balance_check.stderr if balance_check.returncode != 0 else None,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ledger/exists', methods=['GET'])
def ledger_exists():
    """Indique si une organisation a déjà un journal (pour permettre à l'appelant
    de proposer sa création plutôt que de supposer qu'il existe déjà)."""
    org_id = request.args.get('orgId', '')
    try:
        path = org_journal_path(org_id, create=False)
    except NeedsBootstrapError as e:
        return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    return jsonify({'success': True, 'exists': os.path.exists(path)})


DATE_RE = re.compile(r'^\d{4}[/-]\d{2}[/-]\d{2}$')


@app.route('/api/ledger/query', methods=['POST'])
def query():
    """Exécute une commande ledger-cli en lecture seule pour une organisation.

    Body: {orgId, command: "balance"|"register"|"equity"|"print"|"accounts", filters?: [str],
           endDate?: "YYYY/MM/DD" (solde à une date donnée, pour calculer une variation —
           passé en `--end` de façon contrôlée, jamais via `filters` qui interdit tout ce qui
           commence par "-" pour empêcher l'injection de flags arbitraires dans le subprocess)}
    """
    try:
        data = request.get_json() or {}

        org_id = data.get('orgId', '')
        command = data.get('command', 'balance')
        filters = data.get('filters') or []
        end_date = data.get('endDate')

        if command not in QUERY_COMMANDS:
            return jsonify({'success': False, 'error': f'Commande non autorisée: {command}'}), 400

        if not isinstance(filters, list) or any(
            (not isinstance(f, str)) or f.startswith('-') for f in filters
        ):
            return jsonify({'success': False, 'error': 'filters invalides'}), 400

        begin_date = data.get('beginDate')
        date_args = []
        if begin_date is not None:
            begin_date = str(begin_date).replace('-', '/')
            if not DATE_RE.match(begin_date):
                return jsonify({'success': False, 'error': 'beginDate invalide (attendu YYYY/MM/DD)'}), 400
            date_args += ['--begin', begin_date]
        if end_date is not None:
            end_date = str(end_date).replace('-', '/')
            if not DATE_RE.match(end_date):
                return jsonify({'success': False, 'error': 'endDate invalide (attendu YYYY/MM/DD)'}), 400
            date_args += ['--end', end_date]

        try:
            path = org_journal_path(org_id, create=False)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if not os.path.exists(path):
            return jsonify({'success': False, 'error': 'Aucun journal pour cette organisation'}), 404

        result = subprocess.run(
            ['ledger', '-f', path, command, *filters, *date_args],
            capture_output=True, text=True, timeout=15
        )

        return jsonify({
            'success': result.returncode == 0,
            'output': result.stdout,
            'error': result.stderr if result.returncode != 0 else None
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


MODULES_DIR = '/home/ubuntu/ledger_api/modules'
STRUCTORY_MODULE_DIR = os.path.join(MODULES_DIR, 'structory_compta')


def _read_json_bricks(directory):
    """Concatène le contenu brut de toutes les briques JSON d'un dossier (BYOS,
    pas de recopie ailleurs — ce dossier est la copie de travail locale des
    briques Drive)."""
    if not os.path.isdir(directory):
        return []
    contents = []
    for name in sorted(os.listdir(directory)):
        if name.endswith('.json'):
            with open(os.path.join(directory, name), encoding='utf-8') as f:
                contents.append(f.read())
    return contents


_contrepartie_cache = {}  # keyed by org_id or '_generic'


def _load_contrepartie_rules(org_id=None):
    """Règles de contre-passation en cascade : Structory → module org → org.
    Même logique que _resolve_account_references() dans pcg_rules.py."""
    cache_key = org_id or '_generic'
    if cache_key in _contrepartie_cache:
        return _contrepartie_cache[cache_key]

    rules = {}

    def _load_dir(directory):
        if not os.path.isdir(directory):
            return
        for name in sorted(os.listdir(directory)):
            if not name.endswith('.json'):
                continue
            with open(os.path.join(directory, name), encoding='utf-8') as f:
                brick = json.load(f)
            c = brick.get('contenu', {})
            if isinstance(c, dict):
                rules.update(c.get('contreparties', {}))

    _load_dir(STRUCTORY_MODULE_DIR)

    if org_id:
        module = _org_module(org_id)
        if module:
            _load_dir(os.path.join(MODULES_DIR, module, 'bricks'))
        _load_dir(os.path.join(ORGS_DIR, org_id, 'bricks'))

    _contrepartie_cache[cache_key] = rules
    return rules


def _get_contrepartie(compte, sens, org_id=None):
    """Retourne (compte_contrepartie, nom) en cherchant la règle la plus précise
    d'abord (préfixe exact) puis par classe décroissante, dans la cascade de l'org."""
    rules = _load_contrepartie_rules(org_id)
    for length in range(len(compte), 0, -1):
        prefix = compte[:length]
        rule = rules.get(prefix, {}).get(sens)
        if rule:
            return rule['compte'], rule['nom']
    return 'Attente:non-ventile', 'À ventiler manuellement'


@app.route('/api/ledger/journal', methods=['GET'])
def journal_view():
    """Vue HTML du journal comptable — toutes les écritures dans l'ordre chronologique."""
    from flask import Response
    org_id = request.args.get('orgId', '')
    try:
        path = org_journal_path(org_id, create=False)
    except NeedsBootstrapError:
        return Response('<p>Journal pas encore initialisé pour cette organisation.</p>', mimetype='text/html'), 404
    except ValueError as e:
        return Response(f'<p>Erreur : {e}</p>', mimetype='text/html'), 400
    if not os.path.exists(path):
        return Response('<p>Aucun journal pour cette organisation.</p>', mimetype='text/html'), 404

    import csv as _csv, io as _io
    result = subprocess.run(
        ['ledger', '-f', path, 'csv'],
        capture_output=True, text=True, timeout=15,
    )

    # Le fichier .ledger n'est pas forcément trié globalement par date (des blocs de
    # transactions ajoutés à des moments différents peuvent se chevaucher) et `ledger csv`
    # réimprime dans l'ordre du fichier, pas un ordre chronologique global — tri explicite
    # nécessaire (bug réel trouvé le 2026-08-11, retour de Stéphane : "en désordre
    # chronologique"). Rangée par rangée d'abord, regroupées par écriture après le tri.
    parsed_rows = []
    for row in _csv.reader(_io.StringIO(result.stdout)):
        if len(row) < 6:
            continue
        date_raw, _, libelle, compte, _, montant_raw = row[0], row[1], row[2], row[3], row[4], row[5]
        if compte in ('998', '999'):
            continue
        try:
            amount = float(montant_raw)
        except ValueError:
            continue
        parsed_rows.append((date_raw, libelle, compte, amount))
    parsed_rows.sort(key=lambda r: r[0])

    rows_html = ''
    prev_lib = None
    for date_raw, libelle, compte, amount in parsed_rows:
        try:
            d = datetime.strptime(date_raw, '%Y/%m/%d')
            date_fmt = d.strftime('%d/%m/%Y')
        except ValueError:
            date_fmt = date_raw
        # Afficher la date et libellé seulement sur la 1ère jambe de chaque écriture (fonctionne
        # même après tri : les jambes d'une même écriture partagent le même libellé et restent
        # groupées consécutivement grâce au tri stable par date).
        is_new = (libelle != prev_lib)
        prev_lib = libelle
        td = date_fmt if is_new else ''
        tl = libelle if is_new else ''
        compte_clean = compte.split(':')[0]
        debit  = f'{amount:.2f}'  if amount > 0 else ''
        credit = f'{-amount:.2f}' if amount < 0 else ''
        bg = '' if is_new else 'background:#f9f9f9'
        rows_html += (f'<tr style="{bg}"><td>{td}</td><td>{tl}</td>'
                      f'<td>{compte_clean}</td><td style="text-align:right;color:#1a6">{debit}</td>'
                      f'<td style="text-align:right;color:#c33">{credit}</td></tr>\n')

    html = f'''<!doctype html><html><head><meta charset="utf-8">
<title>Journal — {org_id}</title>
<style>body{{font-family:monospace;font-size:13px;margin:20px}}
table{{border-collapse:collapse;width:100%}}
th{{background:#2c5f8a;color:#fff;padding:6px 10px;text-align:left}}
td{{padding:4px 10px;border-bottom:1px solid #eee}}
tr:hover td{{background:#fffbe6}}</style></head><body>
<h2>Journal comptable — {org_id}</h2>
<table><thead><tr><th>Date</th><th>Libellé</th><th>Compte</th>
<th style="text-align:right">Débit</th><th style="text-align:right">Crédit</th></tr></thead>
<tbody>{rows_html}</tbody></table></body></html>'''
    return Response(html, mimetype='text/html')


@app.route('/api/ledger/sheet-entry', methods=['POST'])
def sheet_entry():
    """Écriture depuis le sheet Google Sheets (Communicator).

    Le compte est explicite (tab name), le libellé et le montant aussi.
    La contrepartie est déterminée automatiquement via Rule PCG copropriété.

    Body: {orgId, compte, libelle, montant_debit, montant_credit, date: "YYYY/MM/DD"}
    Exactement l'un de montant_debit ou montant_credit doit être > 0.
    """
    try:
        data = request.get_json() or {}
        org_id = data.get('orgId', '')
        compte = (data.get('compte') or '').strip().replace('.0', '')
        libelle = (data.get('libelle') or '').strip()
        date = (data.get('date') or '').replace('-', '/')
        montant_debit = float(data.get('montant_debit') or 0)
        montant_credit = float(data.get('montant_credit') or 0)

        if not org_id:
            return jsonify({'success': False, 'error': 'orgId manquant'}), 400
        if not compte:
            return jsonify({'success': False, 'error': 'compte manquant'}), 400
        if not libelle:
            return jsonify({'success': False, 'error': 'libelle manquant'}), 400
        if not date:
            return jsonify({'success': False, 'error': 'date manquante'}), 400
        if montant_debit == 0 and montant_credit == 0:
            return jsonify({'success': False, 'error': 'montant_debit ou montant_credit requis'}), 400
        if montant_debit > 0 and montant_credit > 0:
            return jsonify({'success': False, 'error': 'un seul montant à la fois (débit OU crédit)'}), 400

        sens = 'debit' if montant_debit > 0 else 'credit'
        montant = montant_debit if sens == 'debit' else montant_credit
        cpt, nom = _get_contrepartie(compte, sens, org_id)

        # Partie double : compte principal + contrepartie
        if sens == 'debit':
            legs = [
                {'compte': compte, 'label': compte, 'amount': round(montant, 2)},
                {'compte': cpt, 'label': nom, 'amount': round(-montant, 2)},
            ]
        else:
            legs = [
                {'compte': cpt, 'label': nom, 'amount': round(montant, 2)},
                {'compte': compte, 'label': compte, 'amount': round(-montant, 2)},
            ]

        try:
            path = org_journal_path(org_id, create=True)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if os.path.exists(path):
            backup_path = path + f'.bak-{datetime.now().strftime("%Y%m%d%H%M%S")}'
            with open(path) as f_in, open(backup_path, 'w') as f_out:
                f_out.write(f_in.read())

        leg_lines = []
        for leg in legs:
            account_part = f"{leg['compte']}:{leg['label']}" if leg['label'] != leg['compte'] else leg['compte']
            leg_lines.append(f"    {account_part:<45}{leg['amount']:>10.2f} EUR")
        block = f"{date} * {libelle}\n" + "\n".join(leg_lines)

        with open(path, 'a') as f:
            f.write('\n' + block + '\n')
        push_journal_to_drive(org_id, path)

        balance_check = subprocess.run(
            ['ledger', '-f', path, 'balance', '--no-total'],
            capture_output=True, text=True, timeout=10,
        )

        log_to_journal(
            org_id, 'sheet-communicator',
            f'Écriture sheet : {montant:.2f}€ {"débit" if sens == "debit" else "crédit"} {compte} — {libelle}',
            [f'Contrepartie : {cpt} ({nom})', f'Date : {date}'],
        )

        return jsonify({
            'success': True,
            'entry': block,
            'contrepartie': {'compte': cpt, 'nom': nom},
            'balanceOk': balance_check.returncode == 0,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _get_account_balance(path, compte, devise='EUR', end_date=None):
    """Solde d'un compte exact (pas ses sous-comptes) dans le journal d'une org, via le vrai
    moteur ledger-cli. Retourne 0.0 si le compte n'existe pas encore (compte jamais mouvementé)
    — comportement normal au tout premier balance-point. `end_date` optionnel (`YYYY/MM/DD`) :
    solde à une date passée plutôt qu'aujourd'hui, utilisé pour les variations (email quotidien
    "plus forte variation sur 30 jours", 2026-07-26)."""
    cmd = ['ledger', '-f', path, 'balance', f'^{re.escape(compte)}$', '--no-total', '--flat']
    if end_date:
        cmd += ['--end', end_date]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    total = 0.0
    for line in result.stdout.splitlines():
        m = re.match(r'^\s*(-?[\d.,]+)\s+([A-Z]{3})\b', line)
        if m and m.group(2) == devise:
            total += float(m.group(1).replace(',', ''))
    return total


def _get_account_last_date(path, compte):
    """Date (YYYY/MM/DD) de la dernière écriture touchant ce compte exact, ou None si le compte
    n'a jamais été mouvementé — utilisé pour l'indicateur "dernière synchronisation" de la vue
    patrimoine (Navigator, 2026-07-26), pas de notion de date stockée séparément : le journal
    ledger-cli est la seule source de vérité, comme pour le solde."""
    result = subprocess.run(
        ['ledger', '-f', path, 'register', f'^{re.escape(compte)}$', '--no-color', '--date-format', '%Y/%m/%d'],
        capture_output=True, text=True, timeout=10,
    )
    last_date = None
    for line in result.stdout.splitlines():
        m = re.match(r'^(\d{4}/\d{2}/\d{2})\b', line.strip())
        if m:
            last_date = m.group(1)
    return last_date


@app.route('/api/ledger/comptes-solde', methods=['POST'])
def comptes_solde():
    """Solde + date de dernière écriture pour une liste de comptes en un seul appel — vue
    patrimoine agrégée (Navigator, 2026-07-26) : évite un aller-retour ledger-cli par compte
    depuis l'appelant (18 comptes = 18 process `ledger` sinon). Ne résout jamais le connector ni
    le mode de synchro (rôle d'Analyzor/Executor, pas de ledger_api) — uniquement des faits
    comptables.

    Body: {orgId, comptes: [{etablissement, nature, titulaire?, devise?}], endDate?}
    `endDate` (YYYY/MM/DD) optionnel : solde de chaque compte à cette date passée plutôt
    qu'aujourd'hui (variation sur N jours, email quotidien 2026-07-26) — `lastDate` (dernière
    écriture) reste toujours calculé sans égard à `endDate`, ça reste un fait global du compte.
    """
    try:
        data = request.get_json() or {}
        org_id = data.get('orgId', '')
        comptes = data.get('comptes') or []
        end_date = (data.get('endDate') or '').replace('-', '/') or None
        if not org_id or not comptes:
            return jsonify({'success': False, 'error': 'orgId et comptes requis'}), 400

        try:
            path = org_journal_path(org_id, create=False)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        results = []
        for c in comptes:
            etablissement = (c.get('etablissement') or '').strip()
            nature = (c.get('nature') or '').strip()
            titulaire = (c.get('titulaire') or '').strip() or None
            produit = (c.get('produit') or '').strip() or None
            devise = (c.get('devise') or 'EUR').strip().upper()
            if not etablissement or not nature:
                results.append({'etablissement': etablissement, 'nature': nature, 'titulaire': titulaire, 'error': 'etablissement/nature manquant'})
                continue
            compte = _resolve_compte_patrimoine(etablissement, nature, titulaire, produit)
            if os.path.exists(path):
                solde = _get_account_balance(path, compte, devise, end_date=end_date)
                last_date = _get_account_last_date(path, compte)
            else:
                solde = 0.0
                last_date = None
            results.append({
                'etablissement': etablissement, 'nature': nature, 'titulaire': titulaire, 'produit': produit,
                'compte': compte, 'solde': solde, 'devise': devise, 'lastDate': last_date,
            })

        return jsonify({'success': True, 'comptes': results})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _prefix_totals_by_currency(path, prefix, end_date=None):
    """Somme, par devise, tous les comptes sous un préfixe (ex. `Actif:Banque`) — jamais de
    conversion entre devises (§0 ARCHITECTURE.md Suivre Mes Comptes : la conversion est un
    problème d'affichage Sheet/GOOGLEFINANCE, jamais calculé côté backend). `end_date`
    optionnel (`YYYY/MM/DD`) pour un solde à une date passée (variation vs veille)."""
    cmd = ['ledger', '-f', path, 'balance', f'^{re.escape(prefix)}', '--no-total', '--flat']
    if end_date:
        cmd += ['--end', end_date]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    totals = {}
    for line in result.stdout.splitlines():
        m = re.match(r'^\s*(-?[\d.,]+)\s+([A-Z]{3})\b', line)
        if m:
            totals[m.group(2)] = totals.get(m.group(2), 0.0) + float(m.group(1).replace(',', ''))
    return totals


@app.route('/api/ledger/patrimoine', methods=['POST'])
def patrimoine_totals():
    """Solde total par devise pour un préfixe de compte (ex. `Actif:Banque`, patrimoine
    Suivre Mes Comptes) — optionnellement à une date passée, pour permettre de calculer une
    variation (ex. rapport quotidien §9 ARCHITECTURE.md).

    Body: {orgId, prefix?: "Actif:Banque", endDate?: "YYYY/MM/DD"}
    """
    try:
        data = request.get_json() or {}
        org_id = data.get('orgId', '')
        prefix = data.get('prefix') or 'Actif:Banque'
        end_date = data.get('endDate')

        if end_date is not None:
            end_date = str(end_date).replace('-', '/')
            if not DATE_RE.match(end_date):
                return jsonify({'success': False, 'error': 'endDate invalide (attendu YYYY/MM/DD)'}), 400

        try:
            path = org_journal_path(org_id, create=False)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if not os.path.exists(path):
            return jsonify({'success': True, 'totals': {}})

        totals = _prefix_totals_by_currency(path, prefix, end_date)
        return jsonify({'success': True, 'totals': totals})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _resolve_compte_patrimoine(etablissement, nature, titulaire=None, produit=None):
    """Établissement + nature (+ titulaire) (+ produit) -> compte patrimoine ledger-cli (Suivre
    Mes Comptes ARCHITECTURE.md §4bis). Dérivation déterministe
    (Actif:Banque:<Établissement>:<Titulaire>:<Nature>[:<Produit>]) — source unique, réutilisée
    par tous les appelants (Executor, Communicator) pour ne jamais dupliquer cette résolution
    ailleurs.

    `titulaire` fait partie de la clé depuis le 2026-07-25 : sans lui, deux comptes du même
    établissement et de la même nature (ex. "Ferme Verte 323" et "Ferme Verte Photovoltaïque",
    tous deux Qonto/courant) se résolvaient au MÊME compte ledger et s'écrasaient l'un l'autre.

    `produit` ajouté le 2026-07-26 : même bug de collision, cette fois avec établissement+nature
    ET titulaire identiques — "Crédit Mutuel SPL Livret Bleu" et "Crédit Mutuel SPL LDD" sont
    tous deux Crédit Mutuel/épargne/EURL SPL (même titulaire, même nature, mais 2 produits
    d'épargne différents chez la même entité). Trouvé en usage réel (saisie manuelle d'un solde
    sur "SPL LDD" qui aurait écrasé "SPL Livret Bleu"). Optionnel et rétrocompatible : absent
    pour tous les comptes qui n'ont jamais eu ce problème (Mercury, Qonto, BCP, les comptes
    courants Crédit Mutuel...), donc leur chemin ledger ne change pas."""
    slug = ''.join(w.capitalize() for w in etablissement.replace('-', ' ').split())
    parts = [f'Actif:Banque:{slug}']
    if titulaire:
        parts.append(''.join(w.capitalize() for w in titulaire.replace('-', ' ').split()))
    parts.append(nature)
    if produit:
        parts.append(''.join(w.capitalize() for w in produit.replace('-', ' ').split()))
    return ':'.join(parts)


@app.route('/api/ledger/time-points', methods=['GET'])
def time_points():
    """Liste les dates distinctes où AU MOINS UN solde a été réellement constaté pour cette org
    (brique "Time", Suivre Mes Comptes, 2026-08-03 — retour de Stéphane : "il me manque la
    brique Time... naviguer dans le time en fonction des précédentes positions existantes").
    Une "position Time" = un vrai constat de solde (saisie manuelle OU synchro connector),
    jamais un point recalculé/interpolé — dérivé directement du grand livre réel
    (`Attente:ajustement-solde`, la contrepartie systématique de tout `balance-point`, voir
    cette fonction plus bas), jamais une notion stockée séparément.
    Query: ?orgId=..."""
    try:
        org_id = request.args.get('orgId', '')
        if not org_id:
            return jsonify({'success': False, 'error': 'orgId manquant'}), 400
        path = org_journal_path(org_id, create=False)
        if not os.path.exists(path):
            return jsonify({'success': True, 'dates': []})

        result = subprocess.run(
            ['ledger', '-f', path, 'register', 'Attente:ajustement-solde', '--no-color', '--date-format', '%Y/%m/%d'],
            capture_output=True, text=True, timeout=10,
        )
        dates = set()
        for line in result.stdout.splitlines():
            m = re.match(r'^(\d{4}/\d{2}/\d{2})\b', line.strip())
            if m:
                dates.add(m.group(1))

        return jsonify({'success': True, 'dates': sorted(dates, reverse=True)})

    except NeedsBootstrapError as e:
        return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _numero_pcg_compte(org_id, compte):
    """Numero PCG (classe 512...) dun compte patrimoine, depuis le registre de
    lorg (orgs/<org_id>/bricks/*.json, contenu.comptes_patrimoine). Regle voulue
    par Stephane: deja present -> on reutilise ; absent -> on alloue le prochain
    numero libre (5129xx) et on lajoute au registre. Retourne le numero (str) ou
    None si lorg na pas de registre."""
    if not org_id or not compte:
        return None
    # Scope : famille SMC uniquement (module suivre_mes_comptes) — jamais la mère Structory
    # ni les sœurs (compta_copro, structory_compta/jdb). Numérotation patrimoine légère
    # (512 banque / 471 attente) réservée à SMC et ses filles (2026-08-15, consigne Stéphane).
    _mp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orgs", org_id, "module.json")
    try:
        with open(_mp, encoding="utf-8") as _mf:
            if json.load(_mf).get("module") != "suivre_mes_comptes":
                return None
    except (OSError, ValueError):
        return None
    bricks_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orgs", org_id, "bricks")
    if not os.path.isdir(bricks_dir):
        return None
    target_path = None; data = None; table = {}
    for name in sorted(os.listdir(bricks_dir)):
        if not name.endswith(".json"):
            continue
        fp = os.path.join(bricks_dir, name)
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        c = d.get("contenu", {})
        if isinstance(c, dict) and "comptes_patrimoine" in c:
            target_path = fp; data = d; table = c["comptes_patrimoine"]; break
    if target_path is None:
        return None
    if compte in table:
        return table[compte]
    import numerotation_pcg as _npcg
    table = _npcg.numeroter([compte], registre=table)  # alloue par nature (512/503/274/275/471)
    data["contenu"]["comptes_patrimoine"] = table
    tmp = target_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, target_path)
    return table[compte]


@app.route('/api/ledger/balance-point', methods=['POST'])
def balance_point():
    """Constate le solde d'un compte patrimoine à une date donnée (Suivre Mes Comptes,
    ARCHITECTURE.md §4bis) — pas une écriture classée : compare le solde observé au solde
    actuellement enregistré dans le journal, et ne poste que l'écart, en contrepartie du
    compte technique `Attente:ajustement-solde`.

    Body: {orgId, solde, devise?: "EUR", date?: "YYYY/MM/DD",
           etablissement + nature + titulaire? (résolution automatique du compte, cas normal —
           titulaire nécessaire dès qu'une org a plusieurs comptes au même établissement/nature)
           OU compte (override explicite, si l'appelant a déjà résolu le compte lui-même)}
    """
    try:
        data = request.get_json() or {}
        org_id = data.get('orgId', '')
        compte = (data.get('compte') or '').strip()
        etablissement = (data.get('etablissement') or '').strip()
        nature = (data.get('nature') or '').strip()
        titulaire = (data.get('titulaire') or '').strip()
        produit = (data.get('produit') or '').strip()
        solde = data.get('solde')
        devise = (data.get('devise') or 'EUR').strip().upper()
        date = (data.get('date') or datetime.now().strftime('%Y/%m/%d')).replace('-', '/')

        if not org_id:
            return jsonify({'success': False, 'error': 'orgId manquant'}), 400
        if not compte:
            if not etablissement or not nature:
                return jsonify({'success': False, 'error': 'compte manquant (ou etablissement + nature)'}), 400
            compte = _resolve_compte_patrimoine(etablissement, nature, titulaire, produit)
        if solde is None:
            return jsonify({'success': False, 'error': 'solde manquant'}), 400
        try:
            solde = float(str(solde).replace(',', '.'))
        except ValueError:
            return jsonify({'success': False, 'error': 'solde invalide'}), 400

        try:
            path = org_journal_path(org_id, create=True)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        solde_actuel = _get_account_balance(path, compte, devise)
        ecart = round(solde - solde_actuel, 2)

        if abs(ecart) < 0.01:
            # `soldeNouveau` toujours présent, même quand rien n'est posté (2026-07-26, bug réel
            # trouvé en usage) : les appelants (panneau latéral Navigator, Executor) lisent
            # `soldeNouveau` pour rafraîchir l'affichage après une saisie — son absence ici
            # faisait retomber le solde affiché à 0 (`undefined` côté JS) alors que le vrai
            # solde du compte était correct dans le journal.
            return jsonify({
                'success': True,
                'compte': compte,
                'ecart': 0.0,
                'soldeActuel': solde_actuel,
                'soldeNouveau': solde_actuel,
                'entry': None,
                'message': 'Aucun écart, rien à poster',
            })

        if os.path.exists(path):
            backup_path = path + f'.bak-{datetime.now().strftime("%Y%m%d%H%M%S")}'
            with open(path) as f_in, open(backup_path, 'w') as f_out:
                f_out.write(f_in.read())

        _num_pcg = _numero_pcg_compte(org_id, compte)
        _suffixe_num = f"  ; N°{_num_pcg}" if _num_pcg else ""
        _num_cp = _numero_pcg_compte(org_id, 'Attente:ajustement-solde')
        _suffixe_cp = f"  ; N°{_num_cp}" if _num_cp else ""
        leg_lines = [
            f"    {compte:<45}{ecart:>10.2f} {devise}{_suffixe_num}",
            f"    {'Attente:ajustement-solde':<45}{-ecart:>10.2f} {devise}{_suffixe_cp}",
        ]
        block = f"{date} * Solde constaté ({solde:.2f} {devise})\n" + "\n".join(leg_lines)

        with open(path, 'a') as f:
            f.write('\n' + block + '\n')
        push_journal_to_drive(org_id, path)

        balance_check = subprocess.run(
            ['ledger', '-f', path, 'balance', '--no-total'],
            capture_output=True, text=True, timeout=10,
        )

        log_to_journal(
            org_id, 'executor',
            f'Point de solde : {compte} = {solde:.2f} {devise} (écart {ecart:+.2f} {devise})',
            [f'Solde précédent enregistré : {solde_actuel:.2f} {devise}', f'Date : {date}'],
        )

        return jsonify({
            'success': True,
            'compte': compte,
            'ecart': ecart,
            'soldeActuel': solde_actuel,
            'soldeNouveau': solde,
            'entry': block,
            'balanceOk': balance_check.returncode == 0,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _write_module_cache(org_id, module):
    """Cache local dérivé du module (jamais la source de vérité — celle-ci est
    `contenu.module` sur la brique Organisation, dans le Drive de l'org). Permet une lecture
    à froid même si Analyzor est momentanément injoignable."""
    if not module:
        return
    try:
        org_dir = os.path.join(ORGS_DIR, org_id)
        os.makedirs(org_dir, exist_ok=True)
        with open(os.path.join(org_dir, 'module.json'), 'w', encoding='utf-8') as f:
            json.dump({'module': module}, f)
    except OSError:
        pass


def _org_module(org_id):
    """Module de compta branché pour cette org. Source unique de vérité : `contenu.module` sur
    la brique Organisation (Drive de l'org, via Analyzor) — décision Stéphane 2026-08-13, le
    module vit en BYOS, pas sur le VPS. `orgs/<org_id>/module.json` n'est plus qu'un cache local
    dérivé, utilisé seulement si Analyzor est injoignable (lecture à froid). Ainsi une org ne
    peut plus avoir un module "fantôme" présent sur le VPS mais absent de son propre storage."""
    module = None
    try:
        resp = requests.get(f'{ANALYZOR_URL}/api/org/{org_id}', timeout=5)
        if resp.ok:
            org = (resp.json() or {}).get('org') or {}
            module = (org.get('contenu') or {}).get('module')
    except requests.RequestException:
        module = None

    if module:
        _write_module_cache(org_id, module)  # rafraîchit le cache dérivé
        return module

    # Repli cache local (Analyzor injoignable, ou org pas encore migrée en BYOS)
    path = os.path.join(ORGS_DIR, org_id, 'module.json')
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('module')
    return None


@app.route('/api/org/<org_id>/module', methods=['GET'])
def org_module(org_id):
    """Module produit branché pour cette org (orgs/<org_id>/module.json) — ex.
    "suivre_mes_comptes" vs "compta_copro". Nécessaire pour la résolution de connector
    (analyzor::resolve_connectors), qui a besoin du module PRODUIT, pas de la hiérarchie
    d'organisation (parent_org_id de subscriptions_api — deux notions de "module" distinctes,
    confondues jusqu'ici côté Navigator : bug réel trouvé le 2026-07-27, aucun connector ne se
    résolvait jamais en usage réel à cause de ça)."""
    return jsonify({'success': True, 'module': _org_module(org_id)})


@app.route('/api/context/structory', methods=['GET'])
def context_structory():
    """Contexte niveau module Structory + niveau organisation (cascade),
    pour un outil (ex: Communicator) qui a besoin de comprendre les règles
    comptables applicables à une organisation donnée. Le niveau PreCogn
    universel est ajouté par l'appelant, pas ici.
    """
    org_id = request.args.get('orgId', '')

    bricks = _read_json_bricks(STRUCTORY_MODULE_DIR)

    module = _org_module(org_id) if org_id else None
    if module:
        bricks += _read_json_bricks(os.path.join(MODULES_DIR, module, 'bricks'))

    return jsonify({
        'success': True,
        'context': '\n\n'.join(bricks)
    })


@app.route('/api/ledger/provision', methods=['POST'])
def provision_org():
    """Provisionne une nouvelle organisation : crée le dossier orgs/<id>/, le journal vide,
    et le module.json avec le module de compta société par défaut.

    Body: {orgId, name?, module?}  — module par défaut: "compta_societe"
    Idempotent : si l'org existe déjà, retourne success sans écraser.
    """
    try:
        data = request.get_json() or {}
        org_id = (data.get('orgId') or '').strip()
        name = (data.get('name') or org_id).strip()
        # 'compta_societe' n'a jamais existé comme dossier réel (seul 'structory_compta' existe,
        # voir modules/) — ancien défaut resté incohérent, causait un module.json pointant vers
        # un dossier absent, donc _get_bricks_raw()/embedding retrieval toujours vide en silence
        # pour toute org provisionnée sans module explicite (bug trouvé 2026-08-08).
        module = (data.get('module') or 'structory_compta').strip()

        try:
            path = org_journal_path(org_id, create=True)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        org_dir = os.path.dirname(path)
        module_path = os.path.join(org_dir, 'module.json')
        if not os.path.exists(module_path):
            with open(module_path, 'w') as f:
                import json as _json
                _json.dump({'module': module, 'name': name}, f)

        log_to_journal(org_id, 'ledger_api', f'Organisation provisionnée : {name}',
                       [f'Module : {module}'])
        return jsonify({'success': True, 'orgId': org_id, 'module': module})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ledger/fec', methods=['POST'])
def export_fec():
    """Export FEC (Fichier des Écritures Comptables) format DGFiP.

    Body: {orgId, exercice: "2025"}
    """
    try:
        data = request.get_json() or {}
        org_id = data.get('orgId', '')
        exercice = str(data.get('exercice', datetime.now().year))

        try:
            path = org_journal_path(org_id, create=False)
        except NeedsBootstrapError as e:
            return jsonify({'success': False, 'errorCode': 'needs_bootstrap', 'folderId': e.folder_id}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if not os.path.exists(path):
            return jsonify({'success': False, 'error': 'Aucun journal pour cette organisation'}), 404

        fec_content = _generate_fec(path, exercice)
        log_to_journal(org_id, 'ledger_api', f'Export FEC exercice {exercice}')

        return jsonify({'success': True, 'fec': fec_content, 'exercice': exercice})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _generate_fec(journal_path, exercice):
    """Génère le contenu FEC (pipe-separated, format DGFiP) depuis un journal ledger-cli."""
    year = exercice[:4]
    header = '|'.join([
        'JournalCode', 'JournalLib', 'EcritureNum', 'EcritureDate',
        'CompteNum', 'CompteLib', 'CompAuxNum', 'CompAuxLib',
        'PieceRef', 'PieceDate', 'EcritureLib',
        'Debit', 'Credit', 'EcritureLet', 'DateLet', 'ValidDate',
        'Montantdevise', 'Idevise'
    ])
    lines = [header]
    ecriture_num = 1

    with open(journal_path, encoding='utf-8') as f:
        content = f.read()

    # Découpe le fichier en blocs transaction (chaque bloc commence par une date)
    blocks = re.split(r'(?=^\d{4}[/-]\d{2}[/-]\d{2})', content, flags=re.MULTILINE)

    for block in blocks:
        block = block.strip()
        if not block or block.startswith(';'):
            continue
        first_line = block.split('\n')[0].strip()
        m = re.match(r'^(\d{4}[/-]\d{2}[/-]\d{2})\s+(.+)$', first_line)
        if not m:
            continue
        date_raw = m.group(1)
        if not date_raw.startswith(year):
            continue
        payee = m.group(2).split(';')[0].strip()[:32]
        date_fec = date_raw.replace('/', '').replace('-', '')

        postings = []
        for line in block.split('\n')[1:]:
            s = line.strip()
            if not s or s.startswith(';'):
                continue
            parts = re.split(r'\s{2,}', s)
            if len(parts) >= 2:
                account = parts[0].strip()
                amt_str = re.sub(r'[A-Z€$£]+', '', parts[-1]).replace(',', '.').strip()
                try:
                    postings.append({'account': account, 'amount': float(amt_str)})
                except ValueError:
                    pass

        for p in postings:
            amount = p['amount']
            debit  = f"{amount:.2f}".replace('.', ',') if amount > 0 else '0,00'
            credit = f"{-amount:.2f}".replace('.', ',') if amount < 0 else '0,00'
            compte = p['account'].split(':')[0]
            jcode  = _fec_journal_code(compte)
            lines.append('|'.join([
                jcode, _fec_journal_lib(jcode),
                str(ecriture_num).zfill(7),
                date_fec,
                compte, p['account'],
                '', '',
                '', date_fec,
                payee,
                debit, credit,
                '', '', date_fec, '', ''
            ]))
        ecriture_num += 1

    return '\r\n'.join(lines)


def _fec_journal_code(compte):
    c = compte.lstrip('0')[:1] if compte else ''
    return {'5': 'BQ', '6': 'AC', '7': 'VT'}.get(c, 'OD')


def _fec_journal_lib(code):
    return {'BQ': 'Banque', 'AC': 'Achats', 'VT': 'Ventes', 'OD': 'Opérations diverses'}.get(code, 'Journal')


@app.route('/api/ledger/status', methods=['GET'])
def status():
    try:
        ledger_version = subprocess.run(
            ['ledger', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        ).stdout.strip()
    except Exception:
        ledger_version = 'Not installed'

    return jsonify({
        'status': 'online',
        'version': '1.1.0',
        'ledger': ledger_version,
        'timestamp': datetime.now().isoformat(),
        'server': 'vps-03db771f.vps.ovh.net'
    })


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat()
    })



@app.route("/api/ledger/classify", methods=["GET"])
def classify_route():
    """Classification PCG d un libelle (lecture seule, sans ecriture) — expose la fonction
    classify() pour reutilisation par d autres services (ex. jdb_api : suggestion de
    contrepartie sur une proposition avant validation). Params: libelle, sens (recette|
    depense), orgId?."""
    libelle = request.args.get("libelle", "")
    sens = request.args.get("sens", "depense")
    org_id = request.args.get("orgId") or None
    try:
        res = classify(libelle, sens, org_id)
        return jsonify({"success": True, **res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500




@app.route("/api/ledger/journal.json", methods=["GET"])
def journal_json():
    # Journal au format game (riviere) : [{d, l, p:[[compte, montant]]}]. Protege par service-key.
    if request.headers.get("X-Service-Key") not in _ACCEPTED_SERVICE_KEYS:
        return jsonify({"error": "unauthorized"}), 401
    org_id = request.args.get("orgId", "")
    try:
        path = org_journal_path(org_id, create=False)
    except Exception:
        return jsonify([]), 404
    if not os.path.exists(path):
        return jsonify([]), 404
    import csv as _csv, io as _io
    out = subprocess.run(["ledger", "-f", path, "csv"], capture_output=True, text=True, timeout=15).stdout
    rows = []
    for row in _csv.reader(_io.StringIO(out)):
        if len(row) < 6 or row[3] in ("998", "999"):
            continue
        try:
            amt = float(row[5])
        except ValueError:
            continue
        rows.append((row[0].replace("-", "/"), row[2], row[3], amt))
    rows.sort(key=lambda x: x[0])
    ecr = []
    prev = None
    for d, lib, cpt, amt in rows:
        if lib != prev:
            ecr.append({"d": d, "l": lib, "p": []})
            prev = lib
        ecr[-1]["p"].append([cpt, amt])
    return jsonify(ecr)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
