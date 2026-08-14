#!/usr/bin/env python3
"""Règles PCG de base — classification mots-clés -> compte, avec score de confiance.

Ce n'est volontairement pas exhaustif : c'est un point de départ, pensé pour
être complété par un PCG utilisateur (BYOS) plus tard, pas pour couvrir tous
les cas. Une entrée non reconnue reste classée (compte "à vérifier"),
jamais silencieusement ignorée.

Avant de deviner par mots-clés, on cherche d'abord un numéro de compte cité
EXPLICITEMENT dans le libellé (ex: "crédite le 512") — résolu via une table
de référence en donnée (bricks Rule, pas codée en dur ici), cascade
Structory -> module de compta de l'organisation. Une référence explicite
vaut confiance 1.0 : l'utilisateur a nommé le compte, pas de doute possible.
"""

import json
import os
import re
import time

MODULES_DIR = os.path.join(os.path.dirname(__file__), 'modules')
ORGS_DIR = os.path.join(os.path.dirname(__file__), 'orgs')
STRUCTORY_MODULE_DIR = os.path.join(MODULES_DIR, 'structory_compta')

_account_ref_cache = {}
_ACCOUNT_REF_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h : les comptes de référence changent rarement

DEFAULT_COUNTERPART = ("512000", "Banque")

# (mots-clés, compte, libellé du compte, confiance)
PCG_BASE_RULES = [
    (["restaurant", "repas", "déjeuner", "dejeuner"], "625700", "Réceptions", 0.7),
    (["essence", "carburant", "péage", "peage"], "625100", "Frais de déplacement", 0.6),
    (["fourniture", "papeterie"], "606400", "Fournitures administratives", 0.7),
    (["assurance"], "616000", "Primes d'assurance", 0.75),
    (["frais bancaire", "agios", "commission bancaire"], "627000", "Services bancaires", 0.7),
    (["salaire", "paie", "paye"], "641000", "Rémunérations du personnel", 0.8),
    (["loyer"], "613200", "Locations immobilières", 0.8),
    (["électricité", "electricite", "eau", "gaz", "énergie", "energie"], "606100", "Achats non stockés", 0.7),
    (["don ", "dons ", "subvention"], "740000", "Subventions et dons reçus", 0.6),
    (["vente", "prestation", "facture client", "cotisation"], "706000", "Prestations de services", 0.6),
    (["travaux"], "615000", "Entretien et réparations", 0.6),
    (["honoraire", "syndic"], "622600", "Honoraires", 0.65),
]

FALLBACK_DEPENSE = ("606800", "Achats divers — à vérifier", 0.3)
FALLBACK_RECETTE = ("708000", "Produits divers — à vérifier", 0.3)


def _read_json_bricks(directory):
    if not os.path.isdir(directory):
        return []
    bricks = []
    for name in sorted(os.listdir(directory)):
        if name.endswith('.json'):
            with open(os.path.join(directory, name), encoding='utf-8') as f:
                bricks.append(json.load(f))
    return bricks


def _org_module(org_id):
    if not org_id:
        return None
    path = os.path.join(ORGS_DIR, org_id, 'module.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f).get('module')


def _resolve_account_references(org_id=None):
    """Table {numéro: intitulé}, cascade Structory -> module de compta ->
    organisation (le niveau le plus spécifique surcharge). BYOS : lue depuis
    les bricks locales, pas codée en dur. Les comptes bancaires (classe 5)
    en particulier ne peuvent être fixés qu'au niveau organisation — ils
    diffèrent d'une organisation à l'autre."""
    cache_key = org_id or '_generic'
    cached = _account_ref_cache.get(cache_key)
    if cached and (time.time() - cached['t']) < _ACCOUNT_REF_CACHE_TTL_SECONDS:
        return cached['table']

    table = {}
    for brick in _read_json_bricks(STRUCTORY_MODULE_DIR):
        c = brick.get('contenu', {})
        if isinstance(c, dict):
            table.update(c.get('comptes', {}))

    module = _org_module(org_id)
    if module:
        for brick in _read_json_bricks(os.path.join(MODULES_DIR, module, 'bricks')):
            c = brick.get('contenu', {})
            if isinstance(c, dict):
                table.update(c.get('comptes', {}))

    if org_id:
        for brick in _read_json_bricks(os.path.join(ORGS_DIR, org_id, 'bricks')):
            c = brick.get('contenu', {})
            if isinstance(c, dict):
                table.update(c.get('comptes', {}))

    _account_ref_cache[cache_key] = {'table': table, 't': time.time()}
    return table


def _find_explicit_account(libelle, org_id=None):
    """Cherche un numéro de compte cité explicitement dans le libellé
    (ex: "crédite le 512", "compte 606400") et le résout via la table de
    référence. Retourne (compte, nom) ou None si aucun numéro reconnu."""
    table = _resolve_account_references(org_id)

    for number in re.findall(r'\d{3,6}', libelle or ''):
        if number in table:
            return number, table[number]
        for code, nom in table.items():
            if code.startswith(number) or number.startswith(code):
                return code, nom

    return None


def classify(libelle, sens, org_id=None):
    """Retourne {compte, nom, confidence} pour un libellé donné.

    Priorité : (1) numéro de compte cité explicitement (confiance 1.0),
    (2) mots-clés PCG (confiance variable), (3) compte "à vérifier"."""
    explicit = _find_explicit_account(libelle, org_id)
    if explicit:
        compte, nom = explicit
        return {"compte": compte, "nom": nom, "confidence": 1.0}

    libelle_lower = (libelle or "").lower()

    for keywords, compte, nom, confidence in PCG_BASE_RULES:
        if any(k in libelle_lower for k in keywords):
            return {"compte": compte, "nom": nom, "confidence": confidence}

    compte, nom, confidence = FALLBACK_RECETTE if sens == "recette" else FALLBACK_DEPENSE
    return {"compte": compte, "nom": nom, "confidence": confidence}


def build_ledger_entry(date, libelle, montant, sens, compte_info, contrepartie=None):
    """Construit une écriture ledger-cli en partie double, syntaxiquement valide."""
    montant = round(abs(float(montant)), 2)
    compte_contrepartie, nom_contrepartie = contrepartie or DEFAULT_COUNTERPART

    compte_ligne = f"{compte_info['compte']}:{compte_info['nom']}"
    contrepartie_ligne = f"{compte_contrepartie}:{nom_contrepartie}"

    lines = [f"{date} * {libelle}"]

    if sens == "recette":
        lines.append(f"    {contrepartie_ligne}    {montant:.2f} EUR")
        lines.append(f"    {compte_ligne}    -{montant:.2f} EUR")
    else:
        lines.append(f"    {compte_ligne}    {montant:.2f} EUR")
        lines.append(f"    {contrepartie_ligne}    -{montant:.2f} EUR")

    entry_text = "\n".join(lines)

    if compte_info["confidence"] < 0.5:
        entry_text = f"; ⚠ confiance basse ({compte_info['confidence']:.0%}) — compte à vérifier\n" + entry_text

    return entry_text
