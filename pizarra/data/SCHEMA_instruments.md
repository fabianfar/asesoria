# Esquema de data/instruments_*.json

Archivo: { "snapshot": "YYYY-MM-DD", "source": "IOL get_fixed_income_analytics (solo lectura)", "instruments": [ ... ] }

Cada instrumento:
{
  "ticker": "TX28",                      // ticker base de la lámina en pesos (sin D/C)
  "name": "Boncer 2% 2028",              // nombre corto legible
  "issuer": "Tesoro Nacional",
  "family": "soberano_usd | soberano_fija | cer | tamar | provincial | bopreal | letra_lecap | letra_lecer | letra_dlk | on",
  "currency_pay": "USD | ARS",           // moneda en la que paga
  "adjust": "none | CER | TAMAR | BADLAR | DL | other",
  "issue_date": "YYYY-MM-DD" | null,
  "maturity": "YYYY-MM-DD",
  "coupon_text": "2% anual sobre capital ajustado por CER, semestral",
  "fixed_flows": true,                   // false si hay tasa variable/incierta: flows contiene solo fechas y estimaciones marcadas
  "flows_per_100_vn": [                  // por cada 100 VN ORIGINALES, en la moneda de pago, TAL COMO LOS DEVUELVE IOL a la fecha snapshot
    {"date": "YYYY-MM-DD", "interest": 0.0, "amortization": 0.0}
  ],
  "residual_pct": 100.0,                 // % del capital original vigente al snapshot
  "adjust_note": "CER: flows en pesos ajustados por CER del snapshot; para proyectar multiplicar por (1+inflación)^(meses)",
  "quote": {"pesos": "TX28", "usd_mep": "TX28D" | null},   // tickers en data912 para precio
  "price_unit": "per_100_vn",
  "source_note": "IOL analytics DD/MM/YYYY",
  "flags": ["estimado"] // opcional: 'estimado', 'tasa_variable', 'sin_analitica_iol', 'default'
}
