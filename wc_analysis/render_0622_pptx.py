#!/usr/bin/env python3
"""Create a small PPTX deck for the 2026-06-22 World Cup analysis.

This uses only the Python standard library and writes the minimal PowerPoint
Open XML parts needed for a readable deck.
"""
from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

DATA = Path(__file__).parent / "data" / "worldcup_0622_report.json"
OUT = Path(__file__).parent / "outputs" / "worldcup_0622_deep_analysis.pptx"

SLIDE_W = 13_333_500
SLIDE_H = 7_500_000


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def emu(inches: float) -> int:
    return int(inches * 914400)


def text_box(idx: int, x: float, y: float, w: float, h: float, text: str, size: int = 22, bold: bool = False, color: str = "0F172A") -> str:
    runs = []
    for line in text.split("\n"):
        runs.append(
            f"""
            <a:p>
              <a:r>
                <a:rPr lang="zh-CN" sz="{size * 100}" dirty="0"{' b="1"' if bold else ''}>
                  <a:solidFill><a:srgbClr val="{color}"/></a:solidFill>
                </a:rPr>
                <a:t>{escape(line)}</a:t>
              </a:r>
              <a:endParaRPr lang="zh-CN" sz="{size * 100}" dirty="0"/>
            </a:p>"""
        )
    return f"""
    <p:sp>
      <p:nvSpPr><p:cNvPr id="{idx}" name="TextBox {idx}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
      <p:spPr>
        <a:xfrm><a:off x="{emu(x)}" y="{emu(y)}"/><a:ext cx="{emu(w)}" cy="{emu(h)}"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        <a:noFill/><a:ln><a:noFill/></a:ln>
      </p:spPr>
      <p:txBody><a:bodyPr wrap="square"/><a:lstStyle/>{''.join(runs)}</p:txBody>
    </p:sp>"""


def rect(idx: int, x: float, y: float, w: float, h: float, fill: str, line: str = "E2E8F0") -> str:
    return f"""
    <p:sp>
      <p:nvSpPr><p:cNvPr id="{idx}" name="Rect {idx}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr>
        <a:xfrm><a:off x="{emu(x)}" y="{emu(y)}"/><a:ext cx="{emu(w)}" cy="{emu(h)}"/></a:xfrm>
        <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
        <a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>
        <a:ln w="9525"><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>
      </p:spPr>
    </p:sp>"""


def bar_group(start_idx: int, probs: dict[str, float], x: float, y: float) -> str:
    labels = [("主胜", "home", "2563EB"), ("平局", "draw", "64748B"), ("客胜", "away", "DC2626")]
    out = []
    idx = start_idx
    for row, (label, key, color) in enumerate(labels):
        yy = y + row * 0.45
        out.append(text_box(idx, x, yy - 0.03, 0.55, 0.22, label, size=13)); idx += 1
        out.append(rect(idx, x + 0.62, yy, 2.5, 0.13, "E2E8F0", "E2E8F0")); idx += 1
        out.append(rect(idx, x + 0.62, yy, 2.5 * probs[key], 0.13, color, color)); idx += 1
        out.append(text_box(idx, x + 3.25, yy - 0.03, 0.7, 0.22, pct(probs[key]), size=13, bold=True)); idx += 1
    return "".join(out)


def slide_xml(shapes: str, bg: str = "FFFFFF") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="{bg}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree>
    <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
    <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
    {shapes}
  </p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def content_types(slide_count: int) -> str:
    slides = "\n".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  {slides}
</Types>"""


def rels(slide_count: int) -> str:
    slide_rels = "\n".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>'
        for i in range(1, slide_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  {slide_rels}
  <Relationship Id="rId{slide_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  <Relationship Id="rId{slide_count + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>"""


def presentation_xml(slide_count: int) -> str:
    ids = "\n".join(f'<p:sldId id="{255+i}" r:id="rId{i}"/>' for i in range(1, slide_count + 1))
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId{slide_count + 1}"/></p:sldMasterIdLst>
  <p:sldIdLst>{ids}</p:sldIdLst>
  <p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}" type="wide"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>"""


EMPTY_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>"""
ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>"""
MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>"""
MASTER_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>"""
LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"""
LAYOUT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>"""
THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="WorldCup"><a:themeElements><a:clrScheme name="Default"><a:dk1><a:srgbClr val="0F172A"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1E293B"/></a:dk2><a:lt2><a:srgbClr val="F8FAFC"/></a:lt2><a:accent1><a:srgbClr val="2563EB"/></a:accent1><a:accent2><a:srgbClr val="DC2626"/></a:accent2><a:accent3><a:srgbClr val="64748B"/></a:accent3><a:accent4><a:srgbClr val="16A34A"/></a:accent4><a:accent5><a:srgbClr val="F59E0B"/></a:accent5><a:accent6><a:srgbClr val="7C3AED"/></a:accent6><a:hlink><a:srgbClr val="2563EB"/></a:hlink><a:folHlink><a:srgbClr val="7C3AED"/></a:folHlink></a:clrScheme><a:fontScheme name="Default"><a:majorFont><a:latin typeface="Aptos Display"/><a:ea typeface="Microsoft YaHei"/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/><a:ea typeface="Microsoft YaHei"/></a:minorFont></a:fontScheme><a:fmtScheme name="Default"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>"""


def make_slides(report: list[dict]) -> list[str]:
    slides = []
    summary = "\n".join(
        [
            "结论总览",
            "西班牙胜面高，但 -2 深盘不追；",
            "比利时直胜低赔与 -1 均偏贵；",
            "乌拉圭胜最稳，让 -1 接近公平；",
            "埃及胜面在，但新西兰 +1 有保护价值。",
        ]
    )
    shapes = [
        rect(2, 0, 0, 13.33, 1.55, "0F172A", "0F172A"),
        text_box(3, 0.65, 0.35, 8.8, 0.55, "2026-06-22 世界杯四场深度分析", 30, True, "FFFFFF"),
        text_box(4, 0.65, 1.8, 5.7, 2.7, summary, 22, False, "0F172A"),
        text_box(5, 6.6, 1.8, 5.9, 2.7, "方法\n先验：Elo + FBref近期表现 + npxG代理 + Dixon-Coles\n校准：伤病、休息、天气、体彩总进球\n盘口：体彩去水后比较 edge", 18, False, "334155"),
        text_box(6, 0.65, 6.65, 10.5, 0.28, f"生成时间 {datetime.now():%Y-%m-%d %H:%M} · 数据文件 worldcup_0622_report.json", 11, False, "64748B"),
    ]
    slides.append(slide_xml("".join(shapes), bg="F8FAFC"))
    for i, rec in enumerate(report, start=2):
        p = rec["posterior"]["wld"]
        prior = rec["prior"]["wld"]
        top_scores = ", ".join(f"{x['score']} {pct(x['prob'])}" for x in rec["posterior"]["top_scores"][:4])
        reasons = "\n".join(f"• {x}" for x in rec["lambdas"]["calibration_reasons"][:3])
        hcap = rec["market"].get("hhad", {}) if rec.get("market") else {}
        had = rec["market"].get("had", {}) if rec.get("market") else {}
        market_text = "体彩胜平负：" + (
            f"主{pct(had['prob']['home'])}/平{pct(had['prob']['draw'])}/客{pct(had['prob']['away'])}"
            if had else "未开"
        )
        market_text += f"\n让球：{hcap.get('handicap', '未开')}\n热门比分：{top_scores}"
        shapes = [
            text_box(2, 0.55, 0.35, 9.0, 0.5, f"{rec['fixture']['match_num']}  {rec['match']}", 28, True),
            text_box(3, 0.58, 0.92, 8.5, 0.34, f"{rec['fixture']['kickoff_bj']} 北京时间 · {rec['fixture']['venue']}", 13, False, "64748B"),
            rect(4, 10.5, 0.32, 2.1, 0.55, "F1F5F9"),
            text_box(5, 10.72, 0.43, 1.55, 0.22, f"Elo差 {rec['elo']['diff']:+.0f}", 14, True),
            text_box(6, 0.75, 1.65, 3.0, 0.35, "校准后胜平负", 17, True),
            bar_group(7, p, 0.75, 2.15),
            text_box(20, 5.1, 1.65, 3.2, 0.35, "先验 vs 盘口", 17, True),
            text_box(21, 5.1, 2.15, 3.8, 1.45, f"先验：主{pct(prior['home'])} / 平{pct(prior['draw'])} / 客{pct(prior['away'])}\n{market_text}", 14),
            text_box(22, 9.25, 1.65, 3.1, 0.35, "进球期望", 17, True),
            text_box(23, 9.25, 2.15, 3.0, 0.78, f"{rec['lambdas']['posterior_home_lam']:.2f} - {rec['lambdas']['posterior_away_lam']:.2f}", 30, True, "2563EB"),
            text_box(24, 0.75, 4.15, 11.7, 1.45, reasons, 15, False, "334155"),
            text_box(25, 0.75, 6.2, 11.3, 0.4, rec.get("risk_flags", [""])[0] if rec.get("risk_flags") else "", 12, False, "DC2626"),
        ]
        slides.append(slide_xml("".join(shapes), bg="FFFFFF"))
    shapes = [
        text_box(2, 0.7, 0.45, 9.5, 0.55, "数据可靠性与下一步", 30, True),
        text_box(3, 0.85, 1.45, 11.5, 3.7, "可靠性\n• 体彩 API 本轮 live 可抓，matchId 2040247-2040250；临场盘口会变。\n• FBref 用本地缓存 HTML，npxG 是射门/射正代理，不等于真 xG。\n• 天气来自 open-meteo；部分请求可能失败，报告会降级标注。\n• 伤病来自 FIFA/Guardian/AP/RotoWire 等公开源，最终首发仍需临场复核。\n\n建议\n• 开赛前 30-60 分钟重跑脚本刷新体彩和首发。\n• 若你要下注，优先用让球/比分价值，不只看直胜。", 18, False),
        text_box(4, 0.85, 6.45, 10.8, 0.25, "生成脚本：wc_analysis/worldcup_0622_analysis.py / render_0622_pptx.py", 11, False, "64748B"),
    ]
    slides.append(slide_xml("".join(shapes), bg="F8FAFC"))
    return slides


def main() -> None:
    report = json.loads(DATA.read_text(encoding="utf-8"))
    OUT.parent.mkdir(exist_ok=True)
    slides = make_slides(report)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types(len(slides)))
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("docProps/core.xml", f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>2026-06-22 World Cup Analysis</dc:title><dc:creator>Codex</dc:creator><cp:lastModifiedBy>Codex</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}</dcterms:created></cp:coreProperties>""")
        zf.writestr("docProps/app.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>Codex</Application><PresentationFormat>Widescreen</PresentationFormat><Slides>6</Slides></Properties>""")
        zf.writestr("ppt/presentation.xml", presentation_xml(len(slides)))
        zf.writestr("ppt/_rels/presentation.xml.rels", rels(len(slides)))
        zf.writestr("ppt/slideMasters/slideMaster1.xml", MASTER)
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", MASTER_RELS)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", LAYOUT)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", LAYOUT_RELS)
        zf.writestr("ppt/theme/theme1.xml", THEME)
        for i, slide in enumerate(slides, start=1):
            zf.writestr(f"ppt/slides/slide{i}.xml", slide)
            zf.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", EMPTY_RELS)
    print(OUT)


if __name__ == "__main__":
    main()
