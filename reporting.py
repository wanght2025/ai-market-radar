"""Escaped HTML tables and the standalone report document template."""
import html


def table(headers, rows):
    def cell(value):
        if value is None:
            return "缺失"
        return html.escape(f"{value:.2f}" if isinstance(value, float) else str(value))
    return "<div class='scroll'><table><thead><tr>" + "".join(
        f"<th>{cell(x)}</th>" for x in headers) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join(f"<td>{cell(x)}</td>" for x in row) + "</tr>" for row in rows) + "</tbody></table></div>"



def page_html(title: str, heading: str, target: str, status: str, generated: str,
              content: str) -> str:
    label = "核心行情已齐备" if status == "READY" else "本次数据未齐备"
    tone = "ok" if status == "READY" else "bad"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
    <style>
    :root{{--bg:#f6f5f2;--card:#fff;--text:#27292d;--muted:#697078;--line:#deddd8;--good:#6554a5;--warn:#976c29}}
    *{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif}}
    main{{max-width:1100px;margin:auto;padding:32px 18px 64px}}h1{{font-size:clamp(1.8rem,4vw,2.6rem);margin:6px 0}}h2{{font-size:1.2rem;margin:0 0 14px}}
    .eyebrow{{color:var(--good);font-weight:700;letter-spacing:.08em}}.meta{{color:var(--muted);margin:0 0 20px}}
    .card{{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin:16px 0}}
    .banner{{border-left:5px solid var(--warn)}}.banner.ok{{border-color:var(--good)}}.badge{{font-weight:700;border-radius:999px;padding:3px 10px;white-space:nowrap}}
    .badge.ok{{color:#091d15;background:#7ee9b1}}.badge.bad{{color:#271100;background:#ffc78e}}
    table{{border-collapse:collapse;width:100%;min-width:550px}}th,td{{padding:11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
    th{{color:var(--muted);font-weight:600}}.scroll{{overflow-x:auto}}ul{{padding-left:21px}}li{{margin:7px 0;overflow-wrap:anywhere}}
    a{{color:#6554a5}}a:focus-visible,summary:focus-visible{{outline:3px solid #fff;outline-offset:4px}}summary{{cursor:pointer;font-weight:700}}
    progress{{width:100%;height:18px;accent-color:var(--good)}}.action{{display:inline-block;min-height:44px;padding:10px 18px;background:#f0ecf7;border:1px solid #b7abd0;border-radius:8px}}
    code{{font-family:Consolas,monospace;font-size:.85em}}.tasks{{max-height:360px;overflow:auto}}.note{{border-color:#456386}}
    </style></head><body><main><a class="action" href="/">返回研究首页</a><div class="eyebrow">市场雷达 · 存档与采集诊断</div><h1>{html.escape(heading)}</h1>
    <p class="meta">目标交易日 {html.escape(target)} · 生成时间（北京时间） {html.escape(generated)}</p>
    <section class="card banner {tone}" role="status"><strong>{label}</strong><p>{'仅表示行情采集通过。评分、事件与 AI 研究可能仍有缺口，请回到研究首页查看结论。' if status == 'READY' else '不发布新的 核心市场雷达；下方列出缺口，历史报告保留原日期。'}</p></section>
    {content}</main></body></html>"""

