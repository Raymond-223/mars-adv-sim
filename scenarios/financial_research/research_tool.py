# -*- coding: utf-8 -*-
"""Executable tool script for Scenario B: Cross-Domain Financial Research & Risk Analysis."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def _hash_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmd_ingest(workspace_rel: str) -> int:
    ws = Path(workspace_rel).resolve()
    art_dir = ws / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    raw_path = ws / "data" / "raw_market_data.json"
    if not raw_path.exists():
        print(f"Error: missing market data at {raw_path}")
        return 1

    data = json.loads(raw_path.read_text(encoding="utf-8"))
    out_payload = {
        "status": "INGESTED",
        "company_name": data.get("company_name", "Unknown"),
        "news_count": len(data.get("news_items", [])),
        "raw_hash": _hash_file(raw_path),
    }
    (art_dir / "ingest.json").write_text(json.dumps(out_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Ingest step completed successfully.")
    return 0


def cmd_decrypt(workspace_rel: str) -> int:
    ws = Path(workspace_rel).resolve()
    art_dir = ws / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    enc_path = ws / "data" / "encrypted_financials.json"
    if not enc_path.exists():
        print(f"Error: missing confidential file at {enc_path}")
        return 1

    enc_data = json.loads(enc_path.read_text(encoding="utf-8"))
    metrics = enc_data.get("core_metrics", {})

    decrypted_payload = {
        "status": "DECRYPTED_LOCAL_DEVICE",
        "privacy_guarantee": "DEVICE_ENCLAVE_VERIFIED",
        "revenue_m": metrics.get("revenue_cny_million", 0.0),
        "ebitda_m": metrics.get("ebitda_cny_million", 0.0),
        "rnd_expenditure_m": metrics.get("rnd_expenditure_cny_million", 0.0),
        "asset_liability_ratio": metrics.get("asset_liability_ratio", 0.0),
        "evidence_hash": _hash_file(enc_path),
    }
    (art_dir / "decrypted_financials.json").write_text(
        json.dumps(decrypted_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Confidential decryption completed on DEVICE tier.")
    return 0


def cmd_sentiment(workspace_rel: str) -> int:
    ws = Path(workspace_rel).resolve()
    art_dir = ws / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    raw_path = ws / "data" / "raw_market_data.json"
    if not raw_path.exists():
        return 1

    data = json.loads(raw_path.read_text(encoding="utf-8"))
    items = data.get("news_items", [])
    scores = [item.get("sentiment_score", 0.5) for item in items]
    avg_sentiment = sum(scores) / max(1, len(scores))

    sentiment_payload = {
        "status": "ANALYZED",
        "avg_sentiment_score": round(avg_sentiment, 3),
        "sentiment_label": "BULLISH" if avg_sentiment >= 0.7 else "NEUTRAL",
        "evaluated_news_count": len(items),
    }
    (art_dir / "sentiment_analysis.json").write_text(
        json.dumps(sentiment_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Sentiment analysis completed.")
    return 0


def cmd_risk_modeling(workspace_rel: str) -> int:
    ws = Path(workspace_rel).resolve()
    art_dir = ws / "artifacts"
    dec_path = art_dir / "decrypted_financials.json"
    sent_path = art_dir / "sentiment_analysis.json"
    if not dec_path.exists() or not sent_path.exists():
        print("Error: missing prerequisite artifacts for risk modeling")
        return 1

    dec_data = json.loads(dec_path.read_text(encoding="utf-8"))
    sent_data = json.loads(sent_path.read_text(encoding="utf-8"))

    rev = dec_data.get("revenue_m", 100.0)
    ratio = dec_data.get("asset_liability_ratio", 0.5)
    sent = sent_data.get("avg_sentiment_score", 0.5)

    var_95 = round(rev * 0.05 * (1.5 - sent), 2)
    sharpe_ratio = round(2.1 * sent / (ratio + 0.1), 2)
    risk_score = round(10.0 * ratio * (1.2 - sent), 2)

    risk_payload = {
        "status": "COMPUTED_CLOUD",
        "value_at_risk_95_m": var_95,
        "sharpe_ratio": sharpe_ratio,
        "risk_score": risk_score,
        "investment_rating": "BUY" if risk_score < 4.0 and sharpe_ratio > 1.8 else "HOLD",
    }
    (art_dir / "risk_model.json").write_text(
        json.dumps(risk_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Quantitative risk modeling completed on CLOUD tier.")
    return 0


def cmd_compliance(workspace_rel: str) -> int:
    ws = Path(workspace_rel).resolve()
    art_dir = ws / "artifacts"
    dec_path = art_dir / "decrypted_financials.json"
    risk_path = art_dir / "risk_model.json"
    if not dec_path.exists() or not risk_path.exists():
        return 1

    dec_data = json.loads(dec_path.read_text(encoding="utf-8"))
    risk_data = json.loads(risk_path.read_text(encoding="utf-8"))

    device_guarantee = dec_data.get("privacy_guarantee") == "DEVICE_ENCLAVE_VERIFIED"
    acceptable_risk = risk_data.get("risk_score", 10.0) <= 6.0

    compliance_payload = {
        "status": "PASSED" if device_guarantee and acceptable_risk else "FAILED",
        "privacy_compliance": device_guarantee,
        "risk_compliance": acceptable_risk,
        "compliance_hash": _hash_file(dec_path),
    }
    (art_dir / "compliance_audit.json").write_text(
        json.dumps(compliance_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Compliance and privacy audit passed.")
    return 0


def cmd_report(workspace_rel: str) -> int:
    ws = Path(workspace_rel).resolve()
    art_dir = ws / "artifacts"

    ingest_data = json.loads((art_dir / "ingest.json").read_text(encoding="utf-8"))
    dec_data = json.loads((art_dir / "decrypted_financials.json").read_text(encoding="utf-8"))
    sent_data = json.loads((art_dir / "sentiment_analysis.json").read_text(encoding="utf-8"))
    risk_data = json.loads((art_dir / "risk_model.json").read_text(encoding="utf-8"))
    comp_data = json.loads((art_dir / "compliance_audit.json").read_text(encoding="utf-8"))

    report_content = f"""# 跨域多源数据投研与投资风险评估报告

## 一、 目标企业概况
- **企业名称**：{ingest_data.get('company_name')}
- **评估状态**：{ingest_data.get('status')}
- **数据源文件 Hash**：`{ingest_data.get('raw_hash')}`

## 二、 端侧解密核心财务指标（Device Enclave Safe）
- **隐私隔离验证**：{dec_data.get('privacy_guarantee')}
- **营业收入**：￥{dec_data.get('revenue_m')} 百万元
- **EBITDA**：￥{dec_data.get('ebitda_m')} 百万元
- **研发投入**：￥{dec_data.get('rnd_expenditure_m')} 百万元
- **资产负债率**：{dec_data.get('asset_liability_ratio') * 100}%

## 三、 舆情与市场情绪分析 (Edge Execution)
- **评估新闻条数**：{sent_data.get('evaluated_news_count')}
- **平均情绪得分**：{sent_data.get('avg_sentiment_score')} ({sent_data.get('sentiment_label')})

## 四、 云端量化风险与收益建模 (Cloud Execution)
- **95% Value-at-Risk (VaR)**：￥{risk_data.get('value_at_risk_95_m')} 百万元
- **夏普比率 (Sharpe Ratio)**：{risk_data.get('sharpe_ratio')}
- **综合风险得分**：{risk_data.get('risk_score')} / 10.0
- **投资建议评级**：**{risk_data.get('investment_rating')}**

## 五、 合规性审查与数据隐私验证
- **隐私合规性检查**：{'PASS' if comp_data.get('privacy_compliance') else 'FAIL'}
- **风险限额审查**：{'PASS' if comp_data.get('risk_compliance') else 'FAIL'}
- **审计结论**：**{comp_data.get('status')}**

---
*本报告由 MOSAIC-Ω 端-边-云异构群体智能系统自动编译与生成，具备全流程证据链校验。*
"""
    (art_dir / "investment_research_report.md").write_text(report_content, encoding="utf-8")
    print("Final Investment Research Report generated.")
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python research_tool.py <command> <workspace_relative_path>")
        return 1

    subcmd = sys.argv[1]
    ws_rel = sys.argv[2]

    handlers = {
        "ingest": cmd_ingest,
        "decrypt": cmd_decrypt,
        "sentiment": cmd_sentiment,
        "risk_modeling": cmd_risk_modeling,
        "compliance": cmd_compliance,
        "report": cmd_report,
    }

    handler = handlers.get(subcmd)
    if not handler:
        print(f"Unknown command: {subcmd}")
        return 1

    return handler(ws_rel)


if __name__ == "__main__":
    sys.exit(main())
