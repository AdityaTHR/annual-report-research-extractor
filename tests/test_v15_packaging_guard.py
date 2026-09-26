from section_guard_v15 import apply_packaging_guard


def P(a,b,match='GRAPH_CANONICAL'):
    return {'start_page':a,'end_page':b,'semantic_match_type':match}

# Apollo-like: BRR and Risk are body/subheadings inside Directors; CSR is a real
# statutory annexure and is deliberately not blanket-suppressed by this guard.
sections={
    "Directors' Report":P(27,45),
    "Business Responsibility Report (BRR)":P(29,45),
    "Risk Management":P(29,45),
    "Corporate Social Responsibility":P(31,45),
    "Management Discussion & Analysis":P(62,76),
    "Human Resources & Talent":P(76,76),
}
kept,audit=apply_packaging_guard(sections)
assert "Business Responsibility Report (BRR)" not in kept
assert "Risk Management" not in kept
assert "Human Resources & Talent" not in kept
assert "Corporate Social Responsibility" in kept
assert "Directors' Report" in kept
assert "Management Discussion & Analysis" in kept

# Duplicate BRSR/Sustainability range: keep the more specific BRSR.
sections={
    "Business Responsibility & Sustainability Report (BRSR)":P(130,171),
    "Sustainability Report":P(130,171),
}
kept,audit=apply_packaging_guard(sections)
assert "Business Responsibility & Sustainability Report (BRSR)" in kept
assert "Sustainability Report" not in kept

# Manual override always wins.
sections={
    "Management Discussion & Analysis":P(10,20),
    "Risk Management":P(15,16,'MANUAL_OVERRIDE'),
}
kept,audit=apply_packaging_guard(sections)
assert "Risk Management" in kept

print('V15.1 packaging guard tests: PASS')


def test_v15_2_risk_register_card_suppressed():
    from section_guard_v15 import apply_packaging_guard
    sections = {
        "Cybersecurity & IT Governance": {
            "start_page": 41, "end_page": 41,
            "text": "Cyber Security Risk\nRisk Description\nImpact on Value\nMitigating Measure\nCapital at Risk",
            "semantic_match_type": "CYBERSECURITY",
        }
    }
    kept, audit = apply_packaging_guard(sections)
    assert "Cybersecurity & IT Governance" not in kept
    assert any(r.get("reason") == "RISK_REGISTER_CARD_NOT_PEER_REPORT" for r in audit)

test_v15_2_risk_register_card_suppressed()
print('V15.2 risk-card guard: PASS')


def _pg(n, lines):
    out=[]
    for i,item in enumerate(lines):
        if isinstance(item,str):
            item={"text":item}
        row={
            "order":i,
            "text":item.get("text", ""),
            "bbox":item.get("bbox", [50, 50+i*18, 500, 65+i*18]),
            "size":item.get("size", 12.0),
            "font":item.get("font", "Helvetica"),
            "bold":item.get("bold", False),
            "layout_class":item.get("layout_class"),
        }
        out.append(row)
    return {
        "page":n,
        "text":"\n".join(x["text"] for x in out),
        "method":"native",
        "width":595.0,
        "height":842.0,
        "lines":out,
    }


def test_v15_3_recovers_actual_statutory_csr_annexure():
    from section_guard_v15 import repair_nested_boundaries
    pages=[
        _pg(1,["Directors' Report"]),
        _pg(2,["Corporate Social Responsibility", "The CSR report is annexed as Annexure II."]),
        _pg(3,["Directors report body"]),
        _pg(4,["More directors report body"]),
        _pg(5,["Annexure II", "Annual Report on CSR Activities1", "CSR content"]),
        _pg(6,["CSR content continues"]),
        _pg(7,["Annexure III", "Form MGT-9"]),
        _pg(8,["Directors report appendix"]),
    ]
    sections={
        "Directors' Report":P(1,8),
        "Corporate Social Responsibility":{
            **P(2,4),
            "original_heading":"Corporate Social Responsibility",
            "canonical_category":"Corporate Social Responsibility",
            "text":"reference",
            "raw_text":"reference",
        },
    }
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    csr=kept["Corporate Social Responsibility"]
    assert (csr["start_page"],csr["end_page"]) == (5,6)
    assert any(r.get("action") == "RECOVERED_STATUTORY_ANNEXURE" for r in audit)


def test_v15_3_same_style_peer_stops_narrative_csr():
    from section_guard_v15 import repair_nested_boundaries
    style={"size":18.0,"font":"Helvetica-Bold","bold":True,"bbox":[55,70,400,92]}
    pages=[
        _pg(1,["Intro"]),
        _pg(2,[{**style,"text":"Corporate social responsibility"}, "CSR body"]),
        _pg(3,["CSR body continues"]),
        _pg(4,[{**style,"text":"Responsible supply chain"}, "Supply-chain body"]),
        _pg(5,["Supply-chain body continues"]),
        _pg(6,[{**style,"text":"Corporate governance"}, "Governance body"]),
    ]
    sections={
        "Corporate Social Responsibility":{
            **P(2,6),
            "original_heading":"Corporate social responsibility",
            "canonical_category":"Corporate Social Responsibility",
            "text":"csr",
            "raw_text":"csr",
        }
    }
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    csr=kept["Corporate Social Responsibility"]
    assert (csr["start_page"],csr["end_page"]) == (2,3)
    assert any("STRONG_PEER_Responsible supply chain" == r.get("reason") for r in audit)


def test_v15_3_empty_audit_csv_has_header():
    import csv
    import tempfile
    from pathlib import Path
    from batch_v15 import _write_csv
    with tempfile.TemporaryDirectory() as td:
        path=Path(td)/"section_packaging_audit.csv"
        _write_csv(path,[])
        with path.open("r",encoding="utf-8-sig",newline="") as f:
            rows=list(csv.reader(f))
        assert rows and rows[0] == ["section","start_page","end_page","action","reason"]


test_v15_3_recovers_actual_statutory_csr_annexure()
test_v15_3_same_style_peer_stops_narrative_csr()
test_v15_3_empty_audit_csv_has_header()
print('V15.3 CSR recovery + peer-boundary + empty-CSV tests: PASS')


def test_v15_4_csr_wrapper_geometry_and_markup():
    """Real-world layout quirk: Annexure wrapper may be emitted after title in order,
    while bbox places it visually above; title may contain HTML superscript markup."""
    from section_guard_v15 import repair_nested_boundaries
    pages=[
        _pg(1,["Directors' Report"]),
        _pg(2,["Corporate Social Responsibility", "CSR is set out in Annexure II"]),
        _pg(3,["Directors body"]),
        _pg(4,["Directors body"]),
        _pg(5,[
            {"text":"Annual Report on CSR Activities<sup>1</sup>","layout_class":"section-header","bbox":[288,57,432,67],"size":None,"bold":True},
            {"text":"CSR content","bbox":[36,82,500,120]},
            {"text":"Annexure II","layout_class":"folio-edge","bbox":[645,36,684,44],"size":8.0},
        ]),
        _pg(6,["CSR content continues"]),
        _pg(7,[
            {"text":"Annexure III","layout_class":"folio-edge","bbox":[643,39,684,48],"size":8.0},
            "Form MGT-9",
        ]),
    ]
    sections={
        "Directors' Report":P(1,7),
        "Corporate Social Responsibility":{
            **P(2,4),"original_heading":"Corporate Social Responsibility",
            "canonical_category":"Corporate Social Responsibility","text":"ref","raw_text":"ref"
        },
    }
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    csr=kept["Corporate Social Responsibility"]
    assert (csr["start_page"],csr["end_page"]) == (5,6)


def test_v15_4_layout_heading_uses_native_style_duplicate():
    """Do not stop narrative CSR at every same-y section header; require matching
    native typography when layout headings omit size/font metadata."""
    from section_guard_v15 import repair_nested_boundaries
    def page(n, title, native_size, body="body"):
        return _pg(n,[
            {"text":title,"layout_class":"section-header","bbox":[42,79,260,91],"size":None,"font":None,"bold":True},
            {"text":body,"layout_class":"text","bbox":[42,140,520,300],"size":None},
            {"text":title,"layout_class":"folio-edge","bbox":[42.5,76.6,260,91.8],"size":native_size,"font":"Adani-SemiBold","bold":True},
        ])
    pages=[
        _pg(1,["Intro"]),
        page(2,"Corporate social responsibility",12.0),
        page(3,"Expenditure in CSR Focus Areas in FY 2024-25",10.0),
        page(4,"Focus Area – Community Development",11.0),
        page(5,"Responsible supply chain",12.0),
        _pg(6,["Supply chain continues"]),
    ]
    sections={"Corporate Social Responsibility":{
        **P(2,6),"original_heading":"Corporate social responsibility",
        "canonical_category":"Corporate Social Responsibility","text":"csr","raw_text":"csr"
    }}
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    csr=kept["Corporate Social Responsibility"]
    assert (csr["start_page"],csr["end_page"]) == (2,4)
    assert any(r.get("reason") == "STRONG_PEER_Responsible supply chain" for r in audit)


test_v15_4_csr_wrapper_geometry_and_markup()
test_v15_4_layout_heading_uses_native_style_duplicate()
print('V15.4 real-layout CSR guards: PASS')


def test_v15_5_long_form_stops_at_auditor_report():
    from section_guard_v15 import repair_nested_boundaries
    pages=[
        _pg(1,["Business Responsibility Report"]),
        _pg(2,["BRR body"]),
        _pg(3,["BRR body"]),
        _pg(4,[{"text":"Independent Auditor’s Report","layout_class":"section-header","bbox":[50,70,330,92],"bold":True}, "Audit body"]),
        _pg(5,["Financial statements"]),
    ]
    sections={
        "Business Responsibility Report (BRR)":{
            **P(1,5),
            "original_heading":"Business Responsibility Report",
            "canonical_category":"Business Responsibility Report (BRR)",
            "text":"brR",
            "raw_text":"brR",
        }
    }
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    brr=kept["Business Responsibility Report (BRR)"]
    assert (brr["start_page"],brr["end_page"]) == (1,3)
    assert any("Independent Auditor's Report" in r.get("reason","") for r in audit)


def test_v15_5_recovers_chief_executive_review_and_repairs_leadership():
    from section_guard_v15 import repair_nested_boundaries
    top={"size":20.0,"font":"Corp-Bold","bold":True,"bbox":[50,70,300,95]}
    pages=[
        _pg(1,[{"text":"Chairman’s Message","layout_class":"section-header","bbox":[50,70,300,95],"size":None,"font":None,"bold":True},
               {**top,"text":"Chairman’s Message","layout_class":"folio-edge"},"Chairman body"]),
        _pg(2,["Chairman continuation"]),
        _pg(3,[{"text":"Chief Executive’s Review","layout_class":"section-header","bbox":[50,70,320,95],"size":None,"font":None,"bold":True},
               {**top,"text":"Chief Executive’s Review","layout_class":"folio-edge"},"CEO body"]),
        _pg(4,["CEO continuation"]),
        _pg(5,[{**top,"text":"Operating Environment","layout_class":"folio-edge"},"Next chapter"]),
    ]
    sections={
        "Chairman Message":{
            **P(1,5),
            "original_heading":"Chairman’s Message",
            "canonical_category":"Chairman Message",
            "text":"chair",
            "raw_text":"chair",
        }
    }
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    assert (kept["Chairman Message"]["start_page"],kept["Chairman Message"]["end_page"]) == (1,2)
    assert "CEO Message" in kept
    assert (kept["CEO Message"]["start_page"],kept["CEO Message"]["end_page"]) == (3,4)
    assert any(r.get("action") == "RECOVERED_LEADERSHIP_VARIANT" for r in audit)


def test_v15_5_csr_stops_at_new_top_level_awards_page():
    from section_guard_v15 import repair_nested_boundaries
    pages=[
        _pg(1,[
            {"text":"Corporate Social Responsibility","layout_class":"section-header","bbox":[50,70,330,92],"size":20.0,"font":"Corp-Bold","bold":True},
            {"text":"CSR body","layout_class":"text","bbox":[50,140,500,300],"size":None,"font":None},
        ]),
        _pg(2,[{"text":"CSR continuation","layout_class":"text","bbox":[50,80,500,300],"size":None,"font":None}]),
        _pg(3,[{"text":"Awards and Recognitions","layout_class":"section-header","bbox":[50,80,250,100],"size":18.0,"font":"Corp-Bold","bold":True},"Awards body"]),
        _pg(4,["Board of Directors"]),
    ]
    sections={
        "Corporate Social Responsibility":{
            **P(1,3),
            "original_heading":"Corporate Social Responsibility",
            "canonical_category":"Corporate Social Responsibility",
            "text":"csr",
            "raw_text":"csr",
        }
    }
    kept,audit=repair_nested_boundaries(sections,pages,pages)
    csr=kept["Corporate Social Responsibility"]
    assert (csr["start_page"],csr["end_page"]) == (1,2)
    assert any(r.get("reason") == "STRONG_TOP_LEVEL_Awards and Recognitions" for r in audit)


test_v15_5_long_form_stops_at_auditor_report()
test_v15_5_recovers_chief_executive_review_and_repairs_leadership()
test_v15_5_csr_stops_at_new_top_level_awards_page()
print('V15.5 generic boundary hardening: PASS')
