"""V14 semantic normalization layer for the frozen R4.2 extractor.

Keep extractor.py unchanged. This module builds on its validated structural graph,
adds semantic normalization, confidence scoring, safe auto-extraction, traceability,
and raw-extraction caching.
"""

import extractor as core
import csv
import io
import json
import re

# Bind the validated core functions used by the additive V14 layer.
PRESETS = core.PRESETS
_compact = core._compact
_norm_line = core._norm_line
_canonical_request_label = core._canonical_request_label
_looks_like_heading_text = core._looks_like_heading_text
_candidate_has_top_level_evidence = core._candidate_has_top_level_evidence
_detect_section_anchor = core._detect_section_anchor
build_global_section_graph = core.build_global_section_graph
_anchor_key = core._anchor_key
_next_boundary_anchor = core._next_boundary_anchor
_is_two_up_document = core._is_two_up_document
_leadership_end_anchor = core._leadership_end_anchor
_payload_from_anchors = core._payload_from_anchors
clean_pages = core.clean_pages
extract_preset = core.extract_preset
extract_custom = core.extract_custom
extract_source = core.extract_source

# =============================================================================
# V14 — automatic semantic normalization, traceability and extraction caching
# =============================================================================
# This layer is intentionally additive: the validated V13/R4.2 structural engine
# above remains unchanged. V14 classifies only structural candidates produced by
# that engine (plus strongly-evidenced semantic variants), rather than scanning
# arbitrary body sentences for keywords.

import gzip
import hashlib
import os
import pickle
from typing import Dict, List, Optional, Tuple

V14_CACHE_SCHEMA = "v14.1-r4.2-semantic-safe-2"
V14_DEFAULT_CACHE_DIR = os.environ.get("ANNUAL_REPORT_CACHE_DIR", ".v14_cache")

V14_CANONICAL_CATEGORIES = {
    "CHAIRMAN_MESSAGE": "Chairman Message",
    "CEO_MESSAGE": "CEO Message",
    "MD_MESSAGE": "Managing Director Message",
    "BOARDS_REPORT": "Board's Report",
    "DIRECTORS_REPORT": "Directors' Report",
    "MDA": "Management Discussion & Analysis",
    "CORPORATE_GOVERNANCE": "Corporate Governance Report",
    "CSR": "Corporate Social Responsibility",
    "BRSR": "Business Responsibility & Sustainability Report (BRSR)",
    "BRR": "Business Responsibility Report (BRR)",
    "ESG": "ESG Report",
    "SUSTAINABILITY": "Sustainability Report",
    "AUDITORS_REPORT": "Independent Auditor's Report",
    "STANDALONE_FINANCIALS": "Standalone Financial Statements",
    "CONSOLIDATED_FINANCIALS": "Consolidated Financial Statements",
    "FINANCIAL_STATEMENTS": "Financial Statements",
    "RISK_MANAGEMENT": "Risk Management",
    "CYBERSECURITY": "Cybersecurity & IT Governance",
    "HUMAN_RESOURCES": "Human Resources & Talent",
    "ABOUT_COMPANY": "About Company",
    "BOARD_OF_DIRECTORS": "Board of Directors",
    "MANAGEMENT_TEAM": "Management Team",
    "PERFORMANCE_HIGHLIGHTS": "Performance Highlights",
    "AWARDS": "Awards",
    "NOTICE": "Notice",
    "GRI_INDEX": "GRI Index",
    "CLINICAL_GOVERNANCE": "Clinical Governance",
}

# Canonical labels already produced by the validated structural graph.
# Default downloadable research categories.  The global graph still discovers
# every structural heading for the semantic manifest, but front-matter/minor
# headings and financial-statement internals are not automatically packaged.
# This keeps the researcher output clean and prevents supplementary headings
# from becoming peer boundaries that can truncate core sections.
V14_PRIMARY_RESEARCH_CATEGORIES = {
    V14_CANONICAL_CATEGORIES["CHAIRMAN_MESSAGE"],
    V14_CANONICAL_CATEGORIES["CEO_MESSAGE"],
    V14_CANONICAL_CATEGORIES["MD_MESSAGE"],
    V14_CANONICAL_CATEGORIES["BOARDS_REPORT"],
    V14_CANONICAL_CATEGORIES["DIRECTORS_REPORT"],
    V14_CANONICAL_CATEGORIES["MDA"],
    V14_CANONICAL_CATEGORIES["CORPORATE_GOVERNANCE"],
    V14_CANONICAL_CATEGORIES["CSR"],
    V14_CANONICAL_CATEGORIES["BRSR"],
    V14_CANONICAL_CATEGORIES["BRR"],
    V14_CANONICAL_CATEGORIES["ESG"],
    V14_CANONICAL_CATEGORIES["SUSTAINABILITY"],
    V14_CANONICAL_CATEGORIES["RISK_MANAGEMENT"],
    V14_CANONICAL_CATEGORIES["CYBERSECURITY"],
    V14_CANONICAL_CATEGORIES["HUMAN_RESOURCES"],
}

_V14_GRAPH_CANONICAL_MAP = {
    "Chairman Message": V14_CANONICAL_CATEGORIES["CHAIRMAN_MESSAGE"],
    "CEO Message": V14_CANONICAL_CATEGORIES["CEO_MESSAGE"],
    "Managing Director Message": V14_CANONICAL_CATEGORIES["MD_MESSAGE"],
    "Board's Report": V14_CANONICAL_CATEGORIES["BOARDS_REPORT"],
    "Directors' Report": V14_CANONICAL_CATEGORIES["DIRECTORS_REPORT"],
    "Management Discussion & Analysis": V14_CANONICAL_CATEGORIES["MDA"],
    "Corporate Governance Report": V14_CANONICAL_CATEGORIES["CORPORATE_GOVERNANCE"],
    "Corporate Social Responsibility": V14_CANONICAL_CATEGORIES["CSR"],
    "BRSR": V14_CANONICAL_CATEGORIES["BRSR"],
    "BRR": V14_CANONICAL_CATEGORIES["BRR"],
    "ESG Report": V14_CANONICAL_CATEGORIES["ESG"],
    "Sustainability Report": V14_CANONICAL_CATEGORIES["SUSTAINABILITY"],
    "Independent Auditor's Report": V14_CANONICAL_CATEGORIES["AUDITORS_REPORT"],
    "Standalone Financial Statements": V14_CANONICAL_CATEGORIES["STANDALONE_FINANCIALS"],
    "Consolidated Financial Statements": V14_CANONICAL_CATEGORIES["CONSOLIDATED_FINANCIALS"],
    "About Company": V14_CANONICAL_CATEGORIES["ABOUT_COMPANY"],
    "Board of Directors": V14_CANONICAL_CATEGORIES["BOARD_OF_DIRECTORS"],
    "Management Team": V14_CANONICAL_CATEGORIES["MANAGEMENT_TEAM"],
    "Performance Highlights": V14_CANONICAL_CATEGORIES["PERFORMANCE_HIGHLIGHTS"],
    "Awards": V14_CANONICAL_CATEGORIES["AWARDS"],
    "Notice": V14_CANONICAL_CATEGORIES["NOTICE"],
    "GRI Index": V14_CANONICAL_CATEGORIES["GRI_INDEX"],
    "Clinical Governance": V14_CANONICAL_CATEGORIES["CLINICAL_GOVERNANCE"],
}

_V14_CONF_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _v14_semantic_clean(text: str) -> str:
    text = _norm_line(text or "").lower().replace("’", "'")
    text = re.sub(r"[_\-/]+", " ", text)
    text = re.sub(r"[^a-z0-9&' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class SemanticNormalizer:
    """Normalize structural annual-report headings into research categories.

    The normalizer does not treat a keyword hit in body prose as a section. It is
    designed to operate on headings already found by the structural graph, or on
    direct semantic candidates that separately pass top-level evidence checks.
    """

    def __init__(self):
        C = V14_CANONICAL_CATEGORIES
        # (canonical, confidence, match_type, patterns, literal aliases)
        self.rules = [
            (
                C["MDA"], "HIGH", "MDA_CORE",
                [
                    r"\bmanagement(?:'s)?\s+(?:discussion|review)\s*(?:and|&)\s*analysis\b",
                    r"\bmanagement\s+analysis\s*(?:and|&)\s*(?:discussion|review)\b",
                    r"\b(?:discussion|analysis)\s*(?:and|&)\s*(?:analysis|discussion)\s+(?:by|of)\s+management\b",
                    r"\bmd\s*&\s*a\b",
                ],
                [
                    "management review and analysis",
                    "management discussion and analysis",
                    "management discussion & analysis",
                    "management's discussion and analysis",
                    "discussion and analysis by management",
                    "analysis and discussion by management",
                ],
            ),
            (
                C["MDA"], "MEDIUM", "MDA_VARIANT",
                [
                    r"\boperating\s+(?:and|&)\s+financial\s+review\b",
                    r"\bfinancial\s+(?:and|&)\s+operating\s+review\b",
                    r"\bmanagement\s+(?:discussion|review)\b",
                    r"\bdiscussion\s+of\s+management\b",
                    r"\bmanagement\s+commentary\b",
                ],
                [
                    "operating and financial review",
                    "financial and operating review",
                    "management discussion",
                    "management review",
                    "discussion of management",
                    "management commentary",
                ],
            ),
            # Business Review is intentionally only MEDIUM and is later required
            # to have hard/TOC-backed structural evidence before auto-extraction.
            (
                C["MDA"], "MEDIUM", "AMBIGUOUS_BUSINESS_REVIEW",
                [r"^business\s+review(?:\s+and\s+outlook)?$"],
                ["business review", "business review and outlook"],
            ),
            (
                C["BOARDS_REPORT"], "HIGH", "BOARD_REPORT",
                [
                    r"\bboard'?s\s+report\b",
                    r"\breport\s+of\s+the\s+board(?:\s+of\s+directors)?\b",
                    r"\bboard\s+of\s+directors'?\s+report\b",
                ],
                ["board's report", "report of the board", "report of the board of directors", "board of directors report"],
            ),
            (
                C["DIRECTORS_REPORT"], "HIGH", "DIRECTORS_REPORT",
                [r"\bdirectors?'?\s+report\b", r"\breport\s+of\s+(?:the\s+)?directors\b"],
                ["directors' report", "directors report", "report of directors", "report of the directors"],
            ),
            (
                C["CORPORATE_GOVERNANCE"], "HIGH", "CORPORATE_GOVERNANCE",
                [r"\bcorporate\s+governance(?:\s+report)?\b", r"\breport\s+on\s+corporate\s+governance\b"],
                ["corporate governance report", "report on corporate governance", "corporate governance"],
            ),
            (
                C["BRSR"], "HIGH", "BRSR",
                [r"\bbusiness\s+responsibility\s+(?:and|&)\s+sustainability(?:\s+report(?:ing)?)?\b", r"\bbrsr\b"],
                ["business responsibility and sustainability report", "business responsibility & sustainability report", "brsr"],
            ),
            (
                C["BRR"], "HIGH", "BRR",
                [r"\bbusiness\s+responsibility\s+report\b", r"\bbrr\b"],
                ["business responsibility report", "brr"],
            ),
            (
                C["ESG"], "HIGH", "ESG",
                [r"\besg\s+(?:report|review|disclosures?)\b", r"\benvironment(?:al)?\s+social\s+(?:and|&)\s+governance\s+report\b"],
                ["esg report", "esg disclosures", "environmental social and governance report"],
            ),
            (
                C["SUSTAINABILITY"], "HIGH", "SUSTAINABILITY",
                [r"\bsustainability\s+(?:report|review|disclosures?)\b", r"\bintegrated\s+and\s+sustainability\s+report\b"],
                ["sustainability report", "sustainability review", "sustainability disclosures"],
            ),
            (
                C["CSR"], "HIGH", "CSR",
                [r"\bcorporate\s+social\s+responsibility(?:\s+(?:report|activities))?\b", r"\bcsr\s+(?:report|activities)\b"],
                ["corporate social responsibility", "corporate social responsibility report", "csr report"],
            ),
            (
                C["AUDITORS_REPORT"], "HIGH", "AUDITOR",
                [
                    r"\bindependent\s+auditors?'?\s+report\b",
                    r"\bstatutory\s+auditors?'?\s+report\b",
                    r"\breport\s+of\s+(?:the\s+)?independent\s+auditors?\b",
                ],
                ["independent auditor's report", "independent auditors report", "statutory auditor's report"],
            ),
            (
                C["STANDALONE_FINANCIALS"], "HIGH", "STANDALONE_FINANCIALS",
                [r"\bstandalone\s+financial\s+statements\b", r"\bstandalone\s+financials\b"],
                ["standalone financial statements", "standalone financials"],
            ),
            (
                C["CONSOLIDATED_FINANCIALS"], "HIGH", "CONSOLIDATED_FINANCIALS",
                [r"\bconsolidated\s+financial\s+statements\b", r"\bconsolidated\s+financials\b"],
                ["consolidated financial statements", "consolidated financials"],
            ),
            (
                C["FINANCIAL_STATEMENTS"], "MEDIUM", "FINANCIAL_STATEMENTS",
                [r"^financial\s+statements$", r"^financials$"],
                ["financial statements"],
            ),
            (
                C["RISK_MANAGEMENT"], "HIGH", "RISK_MANAGEMENT",
                [r"^risk\s+management(?:\s+report)?$", r"\benterprise\s+risk\s+management\b", r"^risk\s+report$"],
                ["risk management", "risk management report", "enterprise risk management", "risk report"],
            ),
            (
                C["CYBERSECURITY"], "HIGH", "CYBERSECURITY",
                [
                    r"\bcyber\s*security\b",
                    r"\bcybersecurity\b",
                    r"\binformation\s+security\b",
                    r"\bdata\s+privacy\s+(?:and|&)\s+cyber\s*security\b",
                ],
                ["cybersecurity", "cyber security", "information security", "cybersecurity and data privacy"],
            ),
            (
                C["CYBERSECURITY"], "MEDIUM", "IT_GOVERNANCE",
                [r"^it\s+governance$", r"\binformation\s+technology\s+governance\b"],
                ["it governance", "information technology governance"],
            ),
            (
                C["HUMAN_RESOURCES"], "HIGH", "HUMAN_RESOURCES",
                [r"\bhuman\s+resources?(?:\s+(?:report|review))?\b", r"\bhuman\s+resource\s+management\b"],
                ["human resources", "human resource management", "human resources report"],
            ),
            (
                C["HUMAN_RESOURCES"], "MEDIUM", "PEOPLE_TALENT",
                [r"^people\s+(?:and|&)\s+culture$", r"^people\s+(?:and|&)\s+talent$", r"^talent\s+management$", r"^human\s+capital$"],
                ["people and culture", "people & culture", "talent management", "human capital"],
            ),
            (
                C["CHAIRMAN_MESSAGE"], "HIGH", "CHAIRMAN_MESSAGE",
                [r"\b(?:message|letter|statement)\s+from\s+the\s+(?:executive\s+)?chair(?:man|person)\b", r"\bchair(?:man|person)'?s\s+(?:message|letter|statement)\b"],
                ["chairman's message", "chairman's letter", "message from the chairman"],
            ),
            (
                C["CEO_MESSAGE"], "HIGH", "CEO_MESSAGE",
                [r"\b(?:message|letter)\s+from\s+the\s+(?:chief\s+executive\s+officer|ceo)\b", r"\bceo'?s\s+(?:message|letter)\b"],
                ["ceo message", "ceo's message", "message from the ceo"],
            ),
            (
                C["MD_MESSAGE"], "HIGH", "MD_MESSAGE",
                [r"\b(?:message|letter)\s+from\s+the\s+managing\s+director\b", r"\bmanaging\s+director'?s\s+(?:message|letter)\b"],
                ["managing director's message", "message from the managing director", "md message"],
            ),
        ]
        self._compiled = []
        for canonical, confidence, match_type, patterns, aliases in self.rules:
            self._compiled.append({
                "canonical": canonical,
                "confidence": confidence,
                "match_type": match_type,
                "patterns": [re.compile(p, re.IGNORECASE) for p in patterns],
                "aliases": list(aliases),
            })

    def literal_aliases(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for rule in self._compiled:
            out.setdefault(rule["canonical"], [])
            for alias in rule["aliases"]:
                if alias not in out[rule["canonical"]]:
                    out[rule["canonical"]].append(alias)
        return out

    def normalize_heading(self, raw_heading: str, graph_label: Optional[str] = None) -> Dict[str, str]:
        raw = _norm_line(raw_heading or graph_label or "")
        graph = _canonical_request_label(graph_label or "")
        if graph in _V14_GRAPH_CANONICAL_MAP:
            return {
                "original_heading": raw or graph,
                "canonical_category": _V14_GRAPH_CANONICAL_MAP[graph],
                "confidence": "HIGH",
                "match_type": "GRAPH_CANONICAL",
            }

        cleaned = _v14_semantic_clean(raw)
        if not cleaned:
            return {
                "original_heading": raw,
                "canonical_category": raw or "Unclassified",
                "confidence": "LOW",
                "match_type": "EMPTY_OR_UNKNOWN",
            }

        # Protect against a common false positive: financial-statement notes named
        # "Financial Risk Management ..." are not the enterprise risk report.
        if re.search(r"\bfinancial\s+risk\s+management\b", cleaned):
            return {
                "original_heading": raw,
                "canonical_category": raw,
                "confidence": "LOW",
                "match_type": "FINANCIAL_NOTE_NOT_ENTERPRISE_RISK",
            }

        # Fast order-insensitive token logic for genuine MDA variants.
        tokens = set(re.findall(r"[a-z]+", cleaned))
        if {"management", "discussion", "analysis"}.issubset(tokens) and len(tokens) <= 12:
            return {
                "original_heading": raw,
                "canonical_category": V14_CANONICAL_CATEGORIES["MDA"],
                "confidence": "HIGH",
                "match_type": "MDA_TOKEN_SET",
            }
        if {"management", "review", "analysis"}.issubset(tokens) and len(tokens) <= 12:
            return {
                "original_heading": raw,
                "canonical_category": V14_CANONICAL_CATEGORIES["MDA"],
                "confidence": "HIGH",
                "match_type": "MDA_TOKEN_SET",
            }

        for rule in self._compiled:
            for pattern in rule["patterns"]:
                if pattern.search(cleaned):
                    return {
                        "original_heading": raw,
                        "canonical_category": rule["canonical"],
                        "confidence": rule["confidence"],
                        "match_type": rule["match_type"],
                    }

        # Preserve a structural heading even when we cannot safely normalize it.
        return {
            "original_heading": raw,
            "canonical_category": raw,
            "confidence": "LOW",
            "match_type": "GENERIC_STRUCTURAL",
        }


SEMANTIC_NORMALIZER = SemanticNormalizer()


def normalize_heading(raw_heading: str) -> Tuple[str, str, str]:
    """Compatibility helper returning (canonical, confidence, match_type)."""
    r = SEMANTIC_NORMALIZER.normalize_heading(raw_heading)
    return r["canonical_category"], r["confidence"], r["match_type"]


def _v14_semantic_candidate_allowed(node, norm, pages) -> bool:
    conf = norm.get("confidence", "LOW")
    match_type = norm.get("match_type", "")

    text = _norm_line(node.get("matched_text", ""))
    low_text = text.lower().replace("’", "'")

    # Existing canonical graph nodes normally inherit R4.2's guards, but a few
    # labels are easily repeated inside auditor/financial material.  Do not
    # promote those references as section starts without a genuine title.
    if match_type == "GRAPH_CANONICAL":
        canonical = norm.get("canonical_category")
        if canonical == V14_CANONICAL_CATEGORIES["AUDITORS_REPORT"] and re.search(r"^annexure\b", low_text):
            return False
        if canonical in {
            V14_CANONICAL_CATEGORIES["STANDALONE_FINANCIALS"],
            V14_CANONICAL_CATEGORIES["CONSOLIDATED_FINANCIALS"],
        } and re.search(r"^(?:report|notes?)\s+(?:on|to)\b", low_text):
            return False
        return True

    if not text or not _looks_like_heading_text(text):
        return False

    # Ambiguous labels such as "Business Review" must be TOC/hard-boundary backed.
    if match_type == "AMBIGUOUS_BUSINESS_REVIEW":
        return bool(node.get("hard_boundary"))

    if conf == "HIGH":
        if node.get("generic_boundary") and not node.get("hard_boundary"):
            return _candidate_has_top_level_evidence(pages, node)
        return True

    if conf == "MEDIUM":
        return _candidate_has_top_level_evidence(pages, node)

    return False


def _v14_direct_semantic_candidates(pages):
    """Find strongly-evidenced variants that were not already canonical graph nodes."""
    candidates = []
    aliases_by_category = SEMANTIC_NORMALIZER.literal_aliases()
    for canonical, aliases in aliases_by_category.items():
        if not aliases:
            continue
        a = _detect_section_anchor(pages, aliases, min_score=60)
        if not a:
            continue
        a = dict(a)
        norm = SEMANTIC_NORMALIZER.normalize_heading(a.get("matched_text", ""), a.get("label"))
        if norm.get("canonical_category") != canonical:
            continue
        if not _candidate_has_top_level_evidence(pages, a):
            continue
        a.setdefault("label", canonical)
        a.setdefault("aliases", aliases)
        a["v14_semantic_direct"] = True
        candidates.append((a, norm))
    return candidates


def _v14_candidate_rank(node, norm):
    source = str(node.get("detection_source") or "")
    source_rank = {
        "annexure-semantic": 6,
        "separate-appendix": 6,
        "toc-logical-boundary": 5,
        "layout": 4,
        "native": 4,
        "ocr-probe": 3,
        "generic-visual": 2,
    }.get(source, 1)
    return (
        _V14_CONF_RANK.get(norm.get("confidence"), 0),
        source_rank,
        1 if node.get("hard_boundary") else 0,
        node.get("score", 0),
        -int(node.get("index", 10**9) or 10**9),
    )


def discover_semantic_headings(pages, include_low_structural: bool = False) -> List[dict]:
    """Return traceable semantic heading records without extracting section text."""
    graph = build_global_section_graph(pages)
    discovered = []
    seen_positions = set()

    for node in graph:
        pos = (node.get("index"), node.get("line_order", 0))
        norm = SEMANTIC_NORMALIZER.normalize_heading(node.get("matched_text", ""), node.get("label"))
        allowed = _v14_semantic_candidate_allowed(node, norm, pages)
        record = {
            **norm,
            "pdf_page": node.get("pdf_page") or pages[node["index"]].get("page") if isinstance(node.get("index"), int) and 0 <= node.get("index") < len(pages) else None,
            "printed_page": node.get("printed_page"),
            "score": node.get("score", 0),
            "source": node.get("detection_source"),
            "hard_boundary": bool(node.get("hard_boundary")),
            "generic": bool(node.get("generic_boundary")),
            "extractable": bool(allowed),
            "_node": dict(node),
        }
        if norm["confidence"] == "LOW":
            record["extractable"] = bool(
                include_low_structural
                and node.get("generic_boundary")
                and node.get("hard_boundary")
                and _looks_like_heading_text(node.get("matched_text", ""))
            )
        discovered.append(record)
        seen_positions.add(pos)

    # Direct variant recovery: only strong top-level evidence is admitted.
    for node, norm in _v14_direct_semantic_candidates(pages):
        pos = (node.get("index"), node.get("line_order", 0))
        if pos in seen_positions:
            continue
        discovered.append({
            **norm,
            "pdf_page": node.get("pdf_page") or pages[node["index"]].get("page") if isinstance(node.get("index"), int) and 0 <= node.get("index") < len(pages) else None,
            "printed_page": node.get("printed_page"),
            "score": node.get("score", 0),
            "source": node.get("detection_source") or "semantic-direct",
            "hard_boundary": bool(node.get("hard_boundary")),
            "generic": bool(node.get("generic_boundary")),
            "extractable": True,
            "_node": dict(node),
        })
        seen_positions.add(pos)

    discovered.sort(key=lambda r: _anchor_key(r["_node"]))
    return discovered


def _v14_extract_with_validated_path(raw_pages, clean_pages_, canonical, semantic_boundary_headings):
    """Reuse R4.2's validated extraction paths whenever a canonical category is known.

    This preserves the frozen benchmark behavior (including separate-enclosure and
    annexure logic). Semantic-start extraction is only the fallback for genuinely
    new wording that the original preset/custom path cannot locate.
    """
    if canonical in PRESETS:
        try:
            return extract_preset(raw_pages, clean_pages_, canonical)
        except Exception:
            return None

    if canonical in {
        "Board's Report", "Directors' Report", "Corporate Governance Report",
        "Corporate Social Responsibility",
    }:
        try:
            # IMPORTANT: keep the validated R4.2 canonical boundary graph isolated.
            # Feeding every V14 semantic heading back as a custom peer can turn a
            # styled/body reference on the first page of a statutory report into
            # an artificial same-page boundary (for example truncating a Directors
            # Report to its title page). Semantic variants are still available as
            # fallback starts below; canonical extraction must use canonical-only
            # boundary evidence.
            return extract_custom(
                raw_pages, clean_pages_, canonical,
                custom_headings=[canonical],
            )
        except Exception:
            return None
    return None


def auto_extract_semantic_sections(raw_pages, clean_pages_=None, include_low_structural: bool = False, include_supplementary: bool = False):
    """Automatically normalize and extract research sections.

    Returns (sections, semantic_manifest_records). All structural candidates stay
    traceable in the manifest. By default only primary research categories are
    packaged; supplementary recognized headings and LOW hard/TOC-backed headings
    are opt-in so they cannot clutter or prematurely terminate core sections.
    """
    clean_pages_ = clean_pages_ if clean_pages_ is not None else clean_pages(raw_pages)
    records = discover_semantic_headings(clean_pages_, include_low_structural=include_low_structural)

    # Pick one best structural start for each normalized category. LOW structural
    # headings are kept individually (deduped by normalized text) rather than merged.
    best = {}
    low_best = {}
    for rec in records:
        if not rec.get("extractable"):
            continue
        node = rec["_node"]
        if rec["confidence"] == "LOW":
            k = _compact(rec["original_heading"])
            if not k:
                continue
            rank = _v14_candidate_rank(node, rec)
            if k not in low_best or rank > low_best[k][0]:
                low_best[k] = (rank, rec)
            continue
        canonical = rec["canonical_category"]
        if not include_supplementary and canonical not in V14_PRIMARY_RESEARCH_CATEGORIES:
            continue
        rank = _v14_candidate_rank(node, rec)
        if canonical not in best or rank > best[canonical][0]:
            best[canonical] = (rank, rec)

    selected = [x[1] for x in best.values()] + [x[1] for x in low_best.values()]
    selected.sort(key=lambda r: _anchor_key(r["_node"]))

    # Promote all accepted semantic variants to peer boundaries for this extraction
    # pass. This allows an unusual but genuine MDA title to terminate the preceding
    # report section without weakening the global R4.2 generic-boundary rules.
    semantic_boundary_headings = []
    for rec in selected:
        if rec["confidence"] in {"HIGH", "MEDIUM"}:
            h = _norm_line(rec.get("original_heading", ""))
            if h and h not in semantic_boundary_headings:
                semantic_boundary_headings.append(h)

    sections = {}
    selected_positions = set()
    for rec in selected:
        start = dict(rec["_node"])
        canonical = rec["canonical_category"]
        requested_label = _canonical_request_label(start.get("label") or canonical)
        # Canonical graph nodes must retain the frozen R4.2 boundary behavior.
        # Only genuinely new semantic variants need the temporary semantic peer
        # headings. This prevents V14 normalization from changing already-validated
        # statutory section ranges.
        boundary_custom_headings = (
            semantic_boundary_headings
            if rec.get("match_type") != "GRAPH_CANONICAL"
            else None
        )
        boundary = _next_boundary_anchor(
            clean_pages_,
            start,
            requested_label=requested_label,
            custom_headings=boundary_custom_headings,
        )
        if canonical in {
            V14_CANONICAL_CATEGORIES["CHAIRMAN_MESSAGE"],
            V14_CANONICAL_CATEGORIES["CEO_MESSAGE"],
            V14_CANONICAL_CATEGORIES["MD_MESSAGE"],
        }:
            preserve_logical_boundary = bool(
                boundary
                and _is_two_up_document(clean_pages_)
                and start.get("printed_page") is not None
                and boundary.get("printed_page") is not None
                and boundary.get("printed_page") > start.get("printed_page")
            )
            if not preserve_logical_boundary:
                boundary = _leadership_end_anchor(clean_pages_, start, boundary, requested_label)

        payload = None
        if rec["confidence"] != "LOW":
            payload = _v14_extract_with_validated_path(
                raw_pages, clean_pages_, canonical, semantic_boundary_headings
            )
        if payload is None:
            payload = _payload_from_anchors(raw_pages, clean_pages_, start, boundary)
        if not payload:
            continue

        if rec["confidence"] == "LOW":
            base_label = f"Discovered - {rec['original_heading']}"
            label = base_label
            n = 2
            while label in sections:
                label = f"{base_label} ({n})"
                n += 1
        else:
            label = canonical

        payload.update({
            "original_heading": rec["original_heading"],
            "canonical_category": canonical,
            "semantic_confidence": rec["confidence"],
            "semantic_match_type": rec["match_type"],
            "semantic_source": rec.get("source"),
            "heading_score": rec.get("score", 0),
        })
        sections[label] = payload
        selected_positions.add((start.get("index"), start.get("line_order", 0)))

    public_manifest = []
    for rec in records:
        node = rec["_node"]
        pos = (node.get("index"), node.get("line_order", 0))
        public_manifest.append({
            "original_heading": rec["original_heading"],
            "canonical_category": rec["canonical_category"],
            "confidence": rec["confidence"],
            "match_type": rec["match_type"],
            "pdf_page": rec.get("pdf_page"),
            "printed_page": rec.get("printed_page"),
            "score": rec.get("score", 0),
            "source": rec.get("source"),
            "hard_boundary": rec.get("hard_boundary", False),
            "generic": rec.get("generic", False),
            "selected_for_extraction": pos in selected_positions,
        })

    return sections, public_manifest


def semantic_manifest_csv(records: List[dict]) -> bytes:
    fields = [
        "original_heading", "canonical_category", "confidence", "match_type",
        "pdf_page", "printed_page", "score", "source", "hard_boundary",
        "generic", "selected_for_extraction",
    ]
    s = io.StringIO()
    w = csv.DictWriter(s, fieldnames=fields)
    w.writeheader()
    for row in records:
        w.writerow({k: row.get(k, "") for k in fields})
    return s.getvalue().encode("utf-8-sig")


def semantic_manifest_json(records: List[dict]) -> bytes:
    return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")


def _v14_cache_key(name: str, data: bytes) -> str:
    h = hashlib.sha256()
    h.update(V14_CACHE_SCHEMA.encode("utf-8"))
    h.update(b"\0")
    h.update(os.path.splitext(name or "")[1].lower().encode("utf-8"))
    h.update(b"\0")
    h.update(data)
    return h.hexdigest()


def load_cached_extraction_bytes(name: str, data: bytes, cache_dir: Optional[str] = None):
    cache_dir = cache_dir or V14_DEFAULT_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, _v14_cache_key(name, data) + ".pkl.gz")
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, dict) and obj.get("schema") == V14_CACHE_SCHEMA:
            return obj.get("pages")
    except Exception:
        return None
    return None


def save_cached_extraction_bytes(name: str, data: bytes, pages, cache_dir: Optional[str] = None):
    cache_dir = cache_dir or V14_DEFAULT_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, _v14_cache_key(name, data) + ".pkl.gz")
    tmp = path + ".tmp"
    try:
        with gzip.open(tmp, "wb", compresslevel=3) as f:
            pickle.dump({"schema": V14_CACHE_SCHEMA, "pages": pages}, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def extract_source_cached(name: str, data: bytes, cache_dir: Optional[str] = None):
    """Cache raw extraction only; all semantic/graph logic reruns on cached pages."""
    cached = load_cached_extraction_bytes(name, data, cache_dir=cache_dir)
    if cached is not None:
        return cached
    pages = extract_source(name, data)
    save_cached_extraction_bytes(name, data, pages, cache_dir=cache_dir)
    return pages
