"""Script snippet registry — shared starter templates for Script nodes.

Central source of truth: the Script-editor modal in the designer fetches
this list from /api/script_snippets and the "Ask Operator" task chip seeds
its default code from here too. When a new sandbox capability is added to
the executor, add a matching snippet here so it's discoverable in the UI.

Each snippet dict:
    id          stable identifier (kebab-case)
    label       short human label shown in the dropdown
    description one-line hint shown as the menu subtitle
    category    grouping ("Operator input", "Barcode", "Vars", ...)
    code        the Python body inserted into the textarea
    timeout     optional override for the Script node's `timeout` property
                when this snippet is used to seed a new node. 0 = unlimited
                (required for prompt_user snippets so the operator has time
                to type without the 30-second default killing the script).
"""

from __future__ import annotations

from typing import Any

SCRIPT_SNIPPETS: list[dict[str, Any]] = [
    {
        "id": "pass-through",
        "label": "Pass data through",
        "description": "Publish the upstream `data` port value unchanged.",
        "category": "Basics",
        "code": "result = data\n",
    },
    {
        "id": "store-to-vars",
        "label": "Store to blackboard",
        "description": "Write `data` into vars[KEY] so later nodes can use var:KEY.",
        "category": "Basics",
        "code": (
            'vars["my_key"] = data\n'
            "result = data\n"
        ),
    },
    {
        "id": "classify-barcode",
        "label": "Classify barcode by suffix",
        "description": "Route downstream IfElse by tagging the barcode as control/sample.",
        "category": "Barcode",
        "code": (
            "# data here should be the upstream ReadBarcode result.\n"
            'result = "control" if data and data.endswith("-CTRL") else "sample"\n'
        ),
    },
    {
        "id": "prompt-operator",
        "label": "Ask operator for a value",
        "description": "Open a modal, block until the operator types something, publish it.",
        "category": "Operator input",
        "code": (
            '# Opens a modal on the designer. The operator has three choices:\n'
            '#   OK     -> returns the typed string\n'
            '#   Ignore -> returns "" and lets the script continue\n'
            '#   Cancel -> raises OperatorCancelled (falls through to the\n'
            '#             standard Retry / Edit & Retry / Abort modal)\n'
            'answer = prompt_user("Please enter a value:", default="")\n'
            'vars["operator_input"] = answer\n'
            'result = answer\n'
        ),
        "timeout": 0,
    },
    {
        "id": "barcode-fallback",
        "label": "Barcode failure fallback",
        "description": "If the upstream ReadBarcode failed, prompt the operator to type it.",
        "category": "Operator input",
        "code": (
            '# Assumes the upstream ReadBarcode node has store_as="BARCODE".\n'
            '# If the scan returned an empty string, pop a dialog.\n'
            'scanned = vars.get("BARCODE", "") or ""\n'
            'if not scanned:\n'
            '    scanned = prompt_user(\n'
            '        "Barcode read failed.\\nPlease type the plate\'s barcode:",\n'
            '    )\n'
            '    vars["BARCODE"] = scanned\n'
            '    # Location the ReadBarcode targeted — adjust to match yours.\n'
            '    if scanned:\n'
            '        plates[6].barcode = scanned\n'
            '        log("Operator-entered barcode for loc 6: " + scanned)\n'
            'result = scanned\n'
        ),
        "timeout": 0,
    },
    {
        "id": "confirm-step",
        "label": "Confirm before continuing",
        "description": "Pause and require the operator to type 'yes' to proceed.",
        "category": "Operator input",
        "code": (
            'reply = prompt_user("Type YES to continue, anything else to abort:", default="")\n'
            'if reply.strip().lower() != "yes":\n'
            '    raise RuntimeError("Operator declined to continue")\n'
            'result = reply\n'
        ),
        "timeout": 0,
    },
    {
        "id": "read-plate-barcode",
        "label": "Read live plate barcode",
        "description": "Grab the barcode currently attached to the plate at a deck location.",
        "category": "Plates",
        "code": (
            'bc = plates[6].barcode\n'
            'log("Plate at 6 barcode: " + (bc or "(none)"))\n'
            'result = bc\n'
        ),
    },
    {
        "id": "library-kaldor-lookup",
        "label": "Kaldor labware-type ID lookup",
        "description": "Library helper: map labware name -> Kaldor type ID. Call from any Script.",
        "category": "Library helpers",
        "code": (
            '# Paste this into the workflow Library (toolbar -> Library\u2026) so\n'
            '# every Script node can call get_kaldor_labware_type_id(name).\n'
            'def get_kaldor_labware_type_id(name):\n'
            '    """Return the Kaldor type ID for a labware name, or "0000"\n'
            '    if the name is not in the catalog."""\n'
            '    return {\n'
            '        "384 Labcyte LP-0200":                10242,\n'
            '        "384 Labcyte PP-0200":                10302,\n'
            '        "1536 Labcyte LP-0400 LDV":           10202,\n'
            '        "96 Micronic MP96-Q1 with 0.5mL Tube":10402,\n'
            '        "96 Micronic M96-3 with 1.10mL Tubes":10732,\n'
            '        "96 Micronic M96-3 with 0.75mL Tubes":10742,\n'
            '        "96 Agilent DWP":                     2000,\n'
            '        "384 ILP PP":                         10862,\n'
            '    }.get(name, "0000")\n'
        ),
    },
    {
        "id": "call-kaldor-lookup",
        "label": "Use the Kaldor lookup (Script body)",
        "description": "Calls get_kaldor_labware_type_id() from the Library on the plate at loc 6.",
        "category": "Library helpers",
        "code": (
            '# Requires get_kaldor_labware_type_id(...) in the workflow Library.\n'
            'name = plates[6].name\n'
            'type_id = get_kaldor_labware_type_id(name)\n'
            'log(f"Kaldor ID for {name!r}: {type_id}")\n'
            'vars["kaldor_type_id"] = type_id\n'
            'result = type_id\n'
        ),
    },
    {
        "id": "library-kaldor-full",
        "label": "Kaldor helpers bundle (paste into Library)",
        "description": "Config + payload builder + POST + send-transfer one-call helper. DRY_RUN on by default.",
        "category": "Library helpers",
        "code": (
            '# ══════════════════════════════════════════════════════════════════\n'
            '# Kaldor integration \u2014 paste into the workflow Library.\n'
            '# Edit the KALDOR_* constants to match your site. Flip\n'
            '# KALDOR_DRY_RUN to False when ready to fire real POSTs.\n'
            '# ══════════════════════════════════════════════════════════════════\n'
            'KALDOR_ENDPOINT = "http://kaldor.example.com/api/transfer"\n'
            'KALDOR_INSTRUMENT_ID = 1234\n'
            'KALDOR_DEFAULT_DILUTION = 1\n'
            'KALDOR_DEFAULT_PATTERN = "Z"\n'
            'KALDOR_DRY_RUN = True\n'
            '\n'
            '_KALDOR_LABWARE_IDS = {\n'
            '    "384 Labcyte LP-0200":                 10242,\n'
            '    "384 Labcyte PP-0200":                 10302,\n'
            '    "1536 Labcyte LP-0400 LDV":            10202,\n'
            '    "96 Micronic MP96-Q1 with 0.5mL Tube": 10402,\n'
            '    "96 Micronic M96-3 with 1.10mL Tubes": 10732,\n'
            '    "96 Micronic M96-3 with 0.75mL Tubes": 10742,\n'
            '    "96 Agilent DWP":                      2000,\n'
            '    "384 ILP PP":                          10862,\n'
            '}\n'
            '\n'
            'def get_kaldor_labware_type_id(name):\n'
            '    return _KALDOR_LABWARE_IDS.get(name, "0000")\n'
            '\n'
            'def build_kaldor_payload(source_barcode, destination_barcode,\n'
            '                         destination_labware_type_id, transfer_volume,\n'
            '                         quadrant=None, dilution_factor=None,\n'
            '                         instrument_id=None, pattern=None):\n'
            '    return {\n'
            '        "instrumentId": instrument_id if instrument_id is not None else KALDOR_INSTRUMENT_ID,\n'
            '        "sourceBarcode": source_barcode,\n'
            '        "destinationBarcode": destination_barcode,\n'
            '        "destinationLabwareTypeId": destination_labware_type_id,\n'
            '        "transferVolume": transfer_volume,\n'
            '        "dilutionFactor": dilution_factor if dilution_factor is not None else KALDOR_DEFAULT_DILUTION,\n'
            '        "pattern": pattern if pattern is not None else KALDOR_DEFAULT_PATTERN,\n'
            '        "quadrant": "" if quadrant is None else str(quadrant),\n'
            '    }\n'
            '\n'
            'def send_kaldor_post(payload, endpoint=None, timeout=10):\n'
            '    url = endpoint if endpoint is not None else KALDOR_ENDPOINT\n'
            '    if KALDOR_DRY_RUN:\n'
            '        log("[kaldor DRY_RUN] POST " + url + " payload=" + json.dumps(payload))\n'
            '        return {"ok": True, "status": 0, "body": "(dry run)"}\n'
            '    import urllib.request\n'
            '    import urllib.error\n'
            '    body = json.dumps(payload).encode("utf-8")\n'
            '    req = urllib.request.Request(url, data=body,\n'
            '        headers={"Content-Type": "application/json"}, method="POST")\n'
            '    try:\n'
            '        with urllib.request.urlopen(req, timeout=timeout) as resp:\n'
            '            text = resp.read().decode("utf-8", errors="replace")\n'
            '            return {"ok": 200 <= resp.status < 300, "status": resp.status, "body": text}\n'
            '    except urllib.error.HTTPError as e:\n'
            '        try:\n'
            '            text = e.read().decode("utf-8", errors="replace")\n'
            '        except Exception:\n'
            '            text = ""\n'
            '        return {"ok": False, "status": e.code, "body": text}\n'
            '    except Exception as e:\n'
            '        return {"ok": False, "status": 0, "body": str(e)}\n'
            '\n'
            'def kaldor_send_transfer(source_barcode, destination_barcode,\n'
            '                         destination_labware_name, transfer_volume,\n'
            '                         quadrant, **kwargs):\n'
            '    type_id = get_kaldor_labware_type_id(destination_labware_name)\n'
            '    payload = build_kaldor_payload(\n'
            '        source_barcode=source_barcode,\n'
            '        destination_barcode=destination_barcode,\n'
            '        destination_labware_type_id=type_id,\n'
            '        transfer_volume=transfer_volume,\n'
            '        quadrant=quadrant,\n'
            '        **kwargs,\n'
            '    )\n'
            '    return send_kaldor_post(payload)\n'
        ),
    },
    {
        "id": "kaldor-end-of-run",
        "label": "Kaldor send: source x destination x quadrant",
        "description": "End-of-run Script: POST one transfer per source-plate/destination/quadrant combo and log results.",
        "category": "Library helpers",
        "code": (
            '# Requires the Kaldor helpers bundle in the workflow Library.\n'
            '# Assumes vars["plateFivebc"], vars["plateEightbc"], and\n'
            '# vars["source_barcodes"] (list) have been populated upstream.\n'
            'plate5_bc = vars.get("plateFivebc",  "") or ""\n'
            'plate8_bc = vars.get("plateEightbc", "") or ""\n'
            'sources   = vars.get("source_barcodes", []) or []\n'
            '\n'
            'dest5_name = plates[5].name if 5 in plates else ""\n'
            'dest8_name = plates[8].name if 8 in plates else ""\n'
            '\n'
            'log("=== Kaldor transfers ===")\n'
            'kaldor_results = []\n'
            'for i, src_bc in enumerate(sources):\n'
            '    quadrant = i + 1\n'
            '    for dest_bc, dest_name, dest_label in [\n'
            '        (plate5_bc, dest5_name, "loc5"),\n'
            '        (plate8_bc, dest8_name, "loc8"),\n'
            '    ]:\n'
            '        if not src_bc or not dest_bc:\n'
            '            log(f"  SKIP src#{quadrant} -> {dest_label}: missing barcode")\n'
            '            continue\n'
            '        resp = kaldor_send_transfer(\n'
            '            source_barcode=src_bc,\n'
            '            destination_barcode=dest_bc,\n'
            '            destination_labware_name=dest_name,\n'
            '            transfer_volume=5,\n'
            '            quadrant=quadrant,\n'
            '        )\n'
            '        status = "OK" if resp["ok"] else f"FAIL ({resp[\'status\']})"\n'
            '        log(f"  src={src_bc} -> {dest_label}={dest_bc} quad={quadrant} -> {status}")\n'
            '        kaldor_results.append({\n'
            '            "source_barcode": src_bc, "destination_barcode": dest_bc,\n'
            '            "destination_location": dest_label, "quadrant": quadrant,\n'
            '            "ok": resp["ok"], "status": resp["status"],\n'
            '        })\n'
            'vars["kaldor_results"] = kaldor_results\n'
            'result = kaldor_results\n'
        ),
    },
]


def get_snippets() -> list[dict[str, Any]]:
    """Return the snippet registry. Mutations by callers don't leak back
    because we shallow-copy each entry."""
    return [dict(s) for s in SCRIPT_SNIPPETS]
