"""
Numérotation PCG générique des comptes de patrimoine — valable pour TOUTE organisation
Structory (pas de table codée par org). Règle voulue par Stéphane (2026-08-15) :
"on fait du général, applicable partout pour tous".

Principe : le numéro dérive de la NATURE du compte (déduite du chemin ledger
Actif:Banque:<Étab>:<Titulaire>:<Nature>[:<Produit>]) et hérite du plan de la mère Structory.
Allocation stable : déjà présent dans le registre de l org -> réutilise ; absent -> crée le
prochain numéro libre de sa classe. Le registre par org est le seul état persistant.

Classe PCG par nature :
  courant, épargne (livrets...)     -> 512  (banque, classe 5)   [bloc par établissement]
  titres                            -> 503  (VMP)
  assurance_vie                     -> 274  (autres immo. financières)
  retraite                          -> 275  (épargne retraite)
  contrepartie Attente:ajustement-solde -> 471 (compte d attente)
Ne s applique QUE si l org est en mode patrimoine seul ; si un module de compta classique est
actif sur l org, la contrepartie devient un vrai compte 6xx/7xx (géré ailleurs, pas ici).
"""

NATURES = ("courant", "épargne", "epargne", "titres", "assurance_vie", "retraite")
CONTREPARTIE = "Attente:ajustement-solde"


def classe_pcg(nature):
    n = (nature or "").lower()
    if n == "titres":
        return "503"
    if n in ("assurance_vie", "assurancevie"):
        return "274"
    if n == "retraite":
        return "275"
    return "512"  # courant, épargne, et défaut = banque


def nature_from_path(path):
    for seg in path.split(":"):
        if seg.lower() in NATURES:
            return seg.lower()
    return None


def etablissement_from_path(path):
    segs = path.split(":")
    return segs[2] if len(segs) > 2 else "?"


def numeroter(accounts, registre=None):
    """accounts : chemins ledger dans l ordre de première apparition.
    registre : {compte: numéro} déjà alloué (réutilisé tel quel). Retourne le registre complété."""
    reg = dict(registre or {})
    # reconstituer l état des blocs/séquences depuis le registre existant
    bank_block, bank_seq, cls_count = {}, {}, {"503": 0, "274": 0, "275": 0}
    for acc, num in reg.items():
        if num.startswith("512") and len(num) >= 6:
            etab = etablissement_from_path(acc)
            blk = int(num[3:-2]); seq = int(num[-2:])
            bank_block[etab] = blk
            bank_seq[etab] = max(bank_seq.get(etab, 0), seq)
        for c in ("503", "274", "275"):
            if num.startswith(c):
                cls_count[c] = max(cls_count[c], int(num[3:] or 0) + 1)
    for acc in accounts:
        if acc in reg:
            continue
        if acc == CONTREPARTIE:
            reg[acc] = "471000"; continue
        c = classe_pcg(nature_from_path(acc))
        if c == "512":
            etab = etablissement_from_path(acc)
            if etab not in bank_block:
                bank_block[etab] = (max(bank_block.values()) + 1) if bank_block else 1
                bank_seq[etab] = 0
            bank_seq[etab] += 1
            reg[acc] = f"512{bank_block[etab]}{bank_seq[etab]:02d}"
        else:
            reg[acc] = f"{c}{cls_count[c]:03d}"
            cls_count[c] += 1
    return reg
