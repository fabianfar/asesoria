#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Genera ../../preview-snapshot.html: copia AUTÓNOMA de index.html con los JSON de data/ incrustados,
para verla sin servidor (doble clic o dentro de una app). Solo biblioteca estándar.

Uso:  python scripts/make_preview.py            (desde la carpeta pizarra/)
      python scripts/make_preview.py --out /ruta/preview.html
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOMBRES = ["market", "news", "status", "instruments_main", "instruments_ons"]
MARCA = "<!--EMBED-->"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT.parent / "preview-snapshot.html"))
    args = ap.parse_args()
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    if MARCA not in html:
        print("ERROR: index.html no tiene el marcador %s" % MARCA)
        return 1
    embed = {}
    for n in NOMBRES:
        p = ROOT / "data" / (n + ".json")
        try:
            embed[n] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print("Aviso: no se pudo leer %s (%s); se omite" % (p.name, e))
    payload = json.dumps(embed, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    bloque = "<script>window.__EMBED=%s;</script>" % payload
    out = html.replace(MARCA, bloque, 1)
    out = out.replace("<title>Pizarra de Mercado AR — Farías Finanzas</title>",
                      "<title>Pizarra de Mercado AR — Farías Finanzas (vista previa)</title>", 1)
    Path(args.out).write_text(out, encoding="utf-8")
    print("Generado %s (%d KB, %d archivos de datos incrustados)" % (args.out, len(out) // 1024, len(embed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
