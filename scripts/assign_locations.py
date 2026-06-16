"""Batch-assign product default locations in Grocy.

Locations (from live Grocy):
  2 = Fridge
  3 = Vorratsschrank
  4 = Tiefkühler
  5 = Gewürzregal
  6 = Keller
  7 = Obst- & Gemüsekorb
"""
import json, urllib.request, re, time, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

env = (Path(__file__).parents[1] / ".env").read_text()
base = re.search(r"FOODBRAIN_GROCY_BASE_URL=(\S+)", env).group(1)
key  = re.search(r"FOODBRAIN_GROCY_API_KEY=(\S+)", env).group(1)
h = {"GROCY-API-KEY": key, "Content-Type": "application/json"}

FRIDGE      = 2
PANTRY      = 3
FREEZER     = 4
SPICE       = 5
CELLAR      = 6
FRUIT_VEG   = 7

# product_id -> location_id
ASSIGNMENTS = {
    # ── Fridge ──────────────────────────────────────────────────────────────
    2:   FRIDGE,   # Milch
    3:   FRIDGE,   # Dijon Mustard
    5:   FRIDGE,   # snackster mini gurken
    6:   FRIDGE,   # Landjoghurt mild 1,5% Fett
    17:  FRIDGE,   # Ingwer
    19:  FRIDGE,   # Sherrytomaten
    42:  FRIDGE,   # Honig-fermentierter Knoblauch
    60:  FRIDGE,   # Nduja Paste
    62:  FRIDGE,   # Körniger Senf
    63:  FRIDGE,   # Salz Dill Gurken
    64:  FRIDGE,   # Bautzner Senf Aufstrich
    65:  FRIDGE,   # Getrocknete Tomaten in Öl (opened)
    66:  FRIDGE,   # Dunkle Misopaste
    70:  FRIDGE,   # Kapern
    71:  FRIDGE,   # feine Cornichons
    72:  FRIDGE,   # Jalapenos eingelegt
    73:  FRIDGE,   # Ancho Chilipaste
    74:  FRIDGE,   # Quitten Gelee
    75:  FRIDGE,   # Kimchi Rotebeete
    76:  FRIDGE,   # Ziegenweichkäse
    77:  FRIDGE,   # Ziegenkäse
    78:  FRIDGE,   # Gesalzene Zitronen
    79:  FRIDGE,   # Kimchi Ketchup
    80:  FRIDGE,   # Gouda
    82:  FRIDGE,   # Habanero Hot Sauce
    83:  FRIDGE,   # Körniger Frischkäse
    84:  FRIDGE,   # Mirakel Whip Salat Creme
    85:  FRIDGE,   # Wasserkefir
    87:  FRIDGE,   # Mozzarella
    88:  FRIDGE,   # Oliven
    89:  FRIDGE,   # Japanische Reis Marinade
    90:  FRIDGE,   # Japanische Zwiebel Dressing
    91:  FRIDGE,   # Japanische Ski so Salat Piercing (dressing)
    92:  FRIDGE,   # Preiselbeeren Marmelade
    94:  FRIDGE,   # Vietnamesisches Dressing
    95:  FRIDGE,   # Mayo
    96:  FRIDGE,   # Vietnamesische Chilisauce
    97:  FRIDGE,   # Vietnamesisch eingelegte Karotten
    98:  FRIDGE,   # Parmesan
    99:  FRIDGE,   # Geräucherter Schinken
    100: FRIDGE,   # Oatly Haferdrink Vanille
    101: FRIDGE,   # Bio Zitrone
    81:  FRIDGE,   # Rote Beete Saft
    104: FRIDGE,   # Rotkohl
    105: FRIDGE,   # Frühlingszwiebeln
    118: FRIDGE,   # Staudensellerie
    119: FRIDGE,   # Kirschsaft
    120: FRIDGE,   # Suppengrün

    # ── Freezer ─────────────────────────────────────────────────────────────
    108: FREEZER,  # Blaubeeren
    109: FREEZER,  # Himbeeren
    110: FREEZER,  # Prinzessbohnen
    111: FREEZER,  # Russische Teigtaschen
    112: FREEZER,  # Salsiccia
    113: FREEZER,  # Blattspinat
    114: FREEZER,  # Kafirblätter
    115: FREEZER,  # Blattspinat 2
    117: FREEZER,  # Sauerkirschen

    # ── Vorratsschrank ──────────────────────────────────────────────────────
    14:  PANTRY,   # Sauerteig Brot
    20:  PANTRY,   # Honig
    21:  PANTRY,   # Dashi
    22:  PANTRY,   # Himbeeressig
    23:  PANTRY,   # Walnussöl
    24:  PANTRY,   # Reisessig
    25:  PANTRY,   # Sriracha Sauce
    26:  PANTRY,   # Worcestershiresauce
    27:  PANTRY,   # Weißweinessig
    28:  PANTRY,   # Rapsöl
    29:  PANTRY,   # Japanischer Reisessig
    30:  PANTRY,   # Agavendicksaft
    31:  PANTRY,   # Trüffelöl
    32:  PANTRY,   # Fischsauce
    33:  PANTRY,   # Luftgetrocknete Steinpilze
    34:  PANTRY,   # Getrocknetes Tomatenpulver
    35:  PANTRY,   # MSG
    37:  PANTRY,   # Scharfes Sesamöl
    43:  PANTRY,   # Kokosöl neutral
    44:  PANTRY,   # Spicy Chili Oil
    45:  PANTRY,   # Aceto Balsamico Reduktion
    46:  PANTRY,   # Hefeflocken
    47:  PANTRY,   # Haselnüsse
    48:  PANTRY,   # Back Soda
    49:  PANTRY,   # Vanillepudding Pulver
    50:  PANTRY,   # Haferflocken grob
    51:  PANTRY,   # Pita Brötchen
    52:  PANTRY,   # Nutella
    53:  PANTRY,   # Erdnussbutter
    54:  PANTRY,   # Apfelmus
    55:  PANTRY,   # Haselnuss Creme
    57:  PANTRY,   # Mock Duck (canned)
    58:  PANTRY,   # Coro Schokoladencreme
    67:  PANTRY,   # Hotsauce aus Sanddorn
    68:  PANTRY,   # rote Currypaste
    69:  PANTRY,   # Silberzwiebeln
    86:  PANTRY,   # Kokoscreme
    93:  PANTRY,   # Leinöl
    106: PANTRY,   # Jasminreis
    107: PANTRY,   # Schwarze Sojabohnen

    # ── Gewürzregal ─────────────────────────────────────────────────────────
    36:  SPICE,    # Japanische Trüffelalgen
    38:  SPICE,    # Zimt
    39:  SPICE,    # Japanischer Pfeffer
    40:  SPICE,    # Szechuan Pfeffer
    41:  SPICE,    # Japanisches Chilipulver
    59:  SPICE,    # Oregano
}

LOC_NAMES = {FRIDGE: "Fridge", PANTRY: "Vorratsschrank", FREEZER: "Tiefkühler",
             SPICE: "Gewürzregal", CELLAR: "Keller", FRUIT_VEG: "Obst- & Gemüsekorb"}

def patch(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=h, method="PUT")
    try:
        urllib.request.urlopen(req)
        return True
    except urllib.request.HTTPError as e:
        print(f"  ERROR {e.code}: {e.read().decode()[:120]}")
        return False

# fetch product names for display
products = {str(p["id"]): p["name"]
            for p in json.loads(urllib.request.urlopen(
                urllib.request.Request(base + "/api/objects/products", headers=h)).read())}

by_loc = {}
for pid, loc in ASSIGNMENTS.items():
    by_loc.setdefault(loc, []).append(pid)

print("Assigning locations...\n")
ok = err = 0
for loc_id, pids in sorted(by_loc.items()):
    print(f"→ {LOC_NAMES[loc_id]}")
    for pid in sorted(pids):
        name = products.get(str(pid), f"id:{pid}")
        success = patch(f"/api/objects/products/{pid}", {"location_id": loc_id})
        status = "✓" if success else "✗"
        print(f"  {status} {name}")
        if success: ok += 1
        else: err += 1
        time.sleep(0.05)  # be gentle

print(f"\nDone: {ok} updated, {err} errors")
