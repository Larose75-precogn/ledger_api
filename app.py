#!/usr/bin/env python3
from flask import Flask, request, jsonify
import subprocess
import json
import os
import re
import tempfile
from datetime import datetime

from pcg_rules import classify, build_ledger_entry

app = Flask(__name__)

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

QUERY_COMMANDS = {'balance', 'bal', 'register', 'reg', 'equity', 'print', 'accounts'}


def org_journal_path(org_id, create=False):
    if not ORG_ID_RE.match(org_id or ''):
        raise ValueError('orgId invalide')

    org_dir = os.path.join(ORGS_DIR, org_id)
    path = os.path.join(org_dir, 'journal.ledger')

    if create and not os.path.exists(path):
        os.makedirs(org_dir, exist_ok=True)
        with open(path, 'w') as f:
            f.write('; Journal créé automatiquement pour org=' + org_id + '\n')

    return path


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
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        compte_info = classify(libelle, sens, org_id)
        entry_text = build_ledger_entry(date, libelle, montant, sens, compte_info)

        with open(path, 'a') as f:
            f.write('\n' + entry_text + '\n')

        # Vérification d'équilibre via le vrai moteur ledger-cli
        balance_check = subprocess.run(
            ['ledger', '-f', path, 'balance', '--no-total'],
            capture_output=True, text=True, timeout=10
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

            blocks.append(f"{date} * {libelle}\n" + "\n".join(leg_lines))

        try:
            path = org_journal_path(org_id, create=True)
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

        balance_check = subprocess.run(
            ['ledger', '-f', path, 'balance', '--no-total'],
            capture_output=True, text=True, timeout=15
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
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    return jsonify({'success': True, 'exists': os.path.exists(path)})


@app.route('/api/ledger/query', methods=['POST'])
def query():
    """Exécute une commande ledger-cli en lecture seule pour une organisation.

    Body: {orgId, command: "balance"|"register"|"equity"|"print"|"accounts", filters?: [str]}
    """
    try:
        data = request.get_json() or {}

        org_id = data.get('orgId', '')
        command = data.get('command', 'balance')
        filters = data.get('filters') or []

        if command not in QUERY_COMMANDS:
            return jsonify({'success': False, 'error': f'Commande non autorisée: {command}'}), 400

        if not isinstance(filters, list) or any(
            (not isinstance(f, str)) or f.startswith('-') for f in filters
        ):
            return jsonify({'success': False, 'error': 'filters invalides'}), 400

        try:
            path = org_journal_path(org_id, create=False)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400

        if not os.path.exists(path):
            return jsonify({'success': False, 'error': 'Aucun journal pour cette organisation'}), 404

        result = subprocess.run(
            ['ledger', '-f', path, command, *filters],
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


def _org_module(org_id):
    """Lit quel module de compta est branché pour cette organisation
    (orgs/<org_id>/module.json), pour savoir quelles briques ajouter
    en plus du niveau Structory générique."""
    path = os.path.join(ORGS_DIR, org_id, 'module.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f).get('module')


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
