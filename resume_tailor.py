"""
Resume Tailor — JD-driven DOCX tailoring
=========================================
Takes a master DOCX resume + a job description and produces a tailored
copy with:
  1. Professional title updated to match the JD role
  2. Summary rewritten by Ollama to target JD keywords
  3. Skills reordered — JD-matching skills surfaced first
  4. Experience bullet points reordered per entry — best JD matches first

The master is NEVER modified. Tailored copies are cached in
~/.resume_tailor_cache/ keyed by sha256(master_path + jd[:600]).

Usage (standalone):
  python resume_tailor.py <master.docx> "<jd text or path>" [output.docx]

Integration:
  from resume_tailor import tailor_resume
  out = tailor_resume(Path("master.docx"), jd_text)  # returns Path to tailored docx
"""

import copy
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn

# ── Constants ────────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".resume_tailor_cache"
OLLAMA_MODEL = "gemma2:2b"

_SECTION_HEADS = {
    "summary":    {"professional summary", "summary", "objective", "profile"},
    "skills":     {"technical skills", "skills", "core competencies", "key skills"},
    "experience": {"professional experience", "experience", "work experience",
                   "employment history"},
    "education":  {"education", "academic background"},
    "projects":   {"projects", "personal projects"},
    "certs":      {"certifications", "certificates", "achievements"},
}

_STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of",
    "with", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "this", "that", "these", "those", "we", "our", "you", "your",
    "experience", "role", "position", "job", "candidate", "team", "work",
    "using", "use", "build", "develop", "design", "implement", "support",
    "manage", "create", "ensure", "provide", "ability", "skills",
    "strong", "excellent", "good", "well", "highly", "prefer", "required",
    "responsibilities", "requirements", "qualifications",
}


# ── JD keyword extraction ─────────────────────────────────────────────────────

def extract_jd_keywords(jd: str) -> dict:
    """
    Returns {
        "title":    str   — best-guess job title from JD
        "skills":   list  — tech keywords sorted by frequency
        "seniority": str  — "senior"|"junior"|"mid"|""
        "raw_words": set  — all significant words for bullet scoring
    }
    """
    # Title: first non-empty line or line with job-title-ish words
    lines = [l.strip() for l in jd.splitlines() if l.strip()]
    title = ""
    title_kws = {"engineer", "developer", "analyst", "architect", "scientist",
                 "manager", "lead", "specialist", "consultant", "designer"}
    for line in lines[:10]:
        if any(k in line.lower() for k in title_kws) and len(line) < 100:
            title = line.strip()
            break
    if not title and lines:
        title = lines[0]

    # Seniority
    jd_lower = jd.lower()
    if any(w in jd_lower for w in ["senior", "sr.", "sr ", "staff", "principal", "lead"]):
        seniority = "senior"
    elif any(w in jd_lower for w in ["junior", "jr.", "jr ", "entry", "associate"]):
        seniority = "junior"
    else:
        seniority = "mid"

    # Tech keywords — alphanumeric tokens, 2+ chars, not stopwords
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9\+\#\.\/\-]{1,30}", jd)
    freq: dict[str, int] = {}
    for tok in tokens:
        w = tok.lower().rstrip("-.")
        if w not in _STOPWORDS and len(w) >= 2:
            freq[w] = freq.get(w, 0) + 1

    # Tech-looking tokens get a boost (contain digits, caps, special chars)
    tech_boost = {
        w for w in freq
        if any(c.isupper() for c in w[1:])
        or any(c.isdigit() for c in w)
        or "." in w or "+" in w or "#" in w
    }
    scored = sorted(freq.items(), key=lambda x: x[1] + (2 if x[0] in tech_boost else 0), reverse=True)
    skills = [w for w, _ in scored[:40]]

    return {
        "title":     title,
        "skills":    skills,
        "seniority": seniority,
        "raw_words": set(freq.keys()),
    }


def _score_text(text: str, jd_words: set) -> int:
    """Count how many JD words appear in a text string."""
    words = set(re.findall(r"[a-z0-9\+\#\.]{2,}", text.lower()))
    return len(words & jd_words)


# ── DOCX structure parser ─────────────────────────────────────────────────────

def _section_of(text: str) -> Optional[str]:
    """Map paragraph text to section key, or None."""
    t = text.strip().lower().rstrip(":").rstrip()
    for key, heads in _SECTION_HEADS.items():
        if t in heads:
            return key
    return None


def _is_job_title_para(para, prev_section: str) -> bool:
    """Heuristic: a Normal/bold paragraph in the experience section that
    contains a separator (|, ,) and looks like 'Title, Company' or 'Title | Company'."""
    if prev_section != "experience":
        return False
    txt = para.text.strip()
    if not txt:
        return False
    # Typically short, contains comma or pipe
    return ("|" in txt or "," in txt) and len(txt) < 160


def parse_resume(doc: Document) -> dict:
    """
    Parse the resume into sections:
    {
      "title_para_idx": int | None,    # paragraph index of title/role line
      "summary_para_idx": int | None,  # paragraph index of summary text
      "skills_para_idxs": [int],       # paragraphs making up skills section
      "skills_table_idx": int | None,  # table index if skills are in a table
      "experience_entries": [          # each job entry
          {
            "title_idx": int,          # paragraph index of "Job Title, Company"
            "bullet_idxs": [int],      # paragraph indices of bullet points
          }
      ],
      "section_head_idxs": {str: int}, # section name → paragraph index
    }
    """
    paras = doc.paragraphs
    result = {
        "title_para_idx":    None,
        "summary_para_idx":  None,
        "skills_para_idxs":  [],
        "skills_table_idx":  None,
        "experience_entries": [],
        "section_head_idxs": {},
    }

    # Para[1] is often the title line (short, contains | or is a role)
    if len(paras) > 1:
        p1 = paras[1].text.strip()
        if p1 and len(p1) < 120 and ("|" in p1 or any(
            k in p1.lower() for k in ["engineer", "analyst", "developer", "scientist",
                                       "architect", "manager", "specialist"]
        )):
            result["title_para_idx"] = 1

    current_section = ""
    current_exp_entry: Optional[dict] = None

    for idx, para in enumerate(paras):
        txt = para.text.strip()
        if not txt:
            continue

        sec = _section_of(txt)
        if sec:
            current_section = sec
            result["section_head_idxs"][sec] = idx
            if current_exp_entry:
                result["experience_entries"].append(current_exp_entry)
                current_exp_entry = None
            continue

        if current_section == "summary" and result["summary_para_idx"] is None:
            result["summary_para_idx"] = idx
            continue

        if current_section == "skills":
            result["skills_para_idxs"].append(idx)
            continue

        if current_section == "experience":
            style = para.style.name.lower()
            is_bullet = "list" in style or txt.startswith("•") or txt.startswith("*")
            is_title  = _is_job_title_para(para, current_section)

            if is_title or (not is_bullet and len(txt) < 160 and ("|" in txt or "," in txt)):
                if current_exp_entry:
                    result["experience_entries"].append(current_exp_entry)
                current_exp_entry = {"title_idx": idx, "bullet_idxs": []}
            elif is_bullet and current_exp_entry is not None:
                current_exp_entry["bullet_idxs"].append(idx)

    if current_exp_entry:
        result["experience_entries"].append(current_exp_entry)

    # Skills in table?
    for ti, tbl in enumerate(doc.tables):
        for row in tbl.rows:
            for cell in row.cells:
                for cp in cell.paragraphs:
                    if _section_of(cp.text) == "skills":
                        result["skills_table_idx"] = ti
    if result["skills_table_idx"] is None and result["skills_para_idxs"]:
        pass  # skills are in paragraphs

    return result


# ── Run-level text replacement (preserves formatting) ─────────────────────────

def _replace_para_text(para, new_text: str):
    """
    Replace the text of a paragraph while preserving the formatting of the
    FIRST run (font, bold, size, color). All other runs are cleared.
    """
    if not para.runs:
        return
    first_run = para.runs[0]
    # Copy formatting from first run before clearing
    bold  = first_run.bold
    size  = first_run.font.size
    color = first_run.font.color.rgb if first_run.font.color and first_run.font.color.type else None
    name  = first_run.font.name

    # Clear all runs
    for run in para.runs:
        run.text = ""
    # Write new text into first run
    first_run.text = new_text
    first_run.bold = bold
    if size:
        first_run.font.size = size
    if color:
        first_run.font.color.rgb = color
    if name:
        first_run.font.name = name


def _replace_table_cell_text(cell, new_text: str):
    """Replace text in a table cell's first paragraph, preserving formatting."""
    if cell.paragraphs:
        _replace_para_text(cell.paragraphs[0], new_text)


# ── Tailoring steps ───────────────────────────────────────────────────────────

def _tailor_title(doc: Document, structure: dict, jd: dict):
    """Replace the title/role line to match the JD role title."""
    idx = structure.get("title_para_idx")
    if idx is None:
        return
    jd_title = jd.get("title", "").strip()
    if not jd_title or len(jd_title) > 120:
        return

    para = doc.paragraphs[idx]
    current = para.text.strip()

    # If current title has " | " format, keep the format but swap the role part
    if " | " in current:
        parts = current.split(" | ")
        # Replace first part (role) with JD title, keep rest (e.g. secondary role)
        parts[0] = jd_title
        new_text = " | ".join(parts)
    else:
        new_text = jd_title

    _replace_para_text(para, new_text)
    print(f"  [Tailor] Title: {current!r} → {new_text!r}")


def _tailor_summary(doc: Document, structure: dict, jd: dict):
    """Rewrite summary paragraph using Ollama to target JD."""
    idx = structure.get("summary_para_idx")
    if idx is None:
        return
    para       = doc.paragraphs[idx]
    current    = para.text.strip()
    if not current:
        return

    jd_skills  = ", ".join(jd["skills"][:15])
    jd_title   = jd.get("title", "")
    seniority  = jd.get("seniority", "")

    prompt = (
        f"Rewrite this resume professional summary to better match the job description.\n"
        f"Keep it to 2-3 sentences. Sound confident and specific. "
        f"Use first-person implied (no 'I'). Match the tone of an ATS-optimized resume.\n\n"
        f"Current summary:\n{current}\n\n"
        f"Target role: {jd_title}\n"
        f"Key skills required by JD: {jd_skills}\n\n"
        f"Rules:\n"
        f"- Start with the role/title like the original\n"
        f"- Naturally weave in 3-5 JD keywords\n"
        f"- Keep roughly the same length\n"
        f"- Output the summary text ONLY — no quotes, no labels\n"
    )

    try:
        import ollama
        resp = ollama.chat(model=OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}])
        new_text = resp.message.content.strip().strip('"').strip("'")
        # Sanity check — must be non-empty and not too different in length
        if new_text and 0.3 < len(new_text) / max(len(current), 1) < 3.5:
            _replace_para_text(para, new_text)
            print(f"  [Tailor] Summary rewritten ({len(current)} → {len(new_text)} chars)")
        else:
            print(f"  [Tailor] Summary: Ollama output rejected (ratio check), keeping original")
    except Exception as e:
        print(f"  [Tailor] Summary: Ollama error ({e}), keeping original")


def _tailor_skills_paras(doc: Document, idxs: list[int], jd_words: set):
    """
    Reorder skill paragraphs — JD-matching ones first.
    Preserves formatting of each paragraph independently.
    """
    if len(idxs) < 2:
        return
    paras = [doc.paragraphs[i] for i in idxs]
    texts = [p.text for p in paras]
    scored = sorted(range(len(texts)), key=lambda i: _score_text(texts[i], jd_words), reverse=True)
    if scored == list(range(len(texts))):
        return  # already in best order
    # Swap text content (not XML structure) to reorder
    reordered = [texts[i] for i in scored]
    for para, new_text in zip(paras, reordered):
        if para.text != new_text:
            _replace_para_text(para, new_text)
    print(f"  [Tailor] Skills reordered ({len(idxs)} lines)")


def _tailor_skills_table(doc: Document, tbl_idx: int, jd_words: set):
    """
    Reorder rows in a skills table — JD-matching rows first.
    Only reorders the VALUE cells (every other column).
    """
    tbl = doc.tables[tbl_idx]
    rows = tbl.rows
    if len(rows) < 2:
        return
    # Each row: [label_cell, value_cell]
    entries = []
    for row in rows:
        cells = row.cells
        if len(cells) >= 2:
            entries.append((cells[0].text, cells[1].text))
        elif len(cells) == 1:
            entries.append((cells[0].text, ""))

    scored = sorted(range(len(entries)), key=lambda i: _score_text(entries[i][1], jd_words), reverse=True)
    if scored == list(range(len(entries))):
        return
    reordered = [entries[i] for i in scored]
    for row, (label, value) in zip(rows, reordered):
        cells = row.cells
        if len(cells) >= 2:
            if cells[1].text != value:
                _replace_table_cell_text(cells[1], value)
            if cells[0].text != label:
                _replace_table_cell_text(cells[0], label)
    print(f"  [Tailor] Skills table reordered ({len(rows)} rows)")


def _tailor_bullets(doc: Document, entries: list[dict], jd_words: set):
    """
    Within each experience entry, reorder bullet points so the ones
    most relevant to the JD come first. Never changes bullet text.
    """
    reordered_count = 0
    for entry in entries:
        bullet_idxs = entry.get("bullet_idxs", [])
        if len(bullet_idxs) < 2:
            continue
        paras = [doc.paragraphs[i] for i in bullet_idxs]
        texts = [p.text for p in paras]
        scored = sorted(range(len(texts)), key=lambda i: _score_text(texts[i], jd_words), reverse=True)
        if scored == list(range(len(texts))):
            continue
        reordered_texts = [texts[i] for i in scored]
        for para, new_text in zip(paras, reordered_texts):
            if para.text.strip() != new_text.strip():
                _replace_para_text(para, new_text)
                reordered_count += 1
    if reordered_count:
        print(f"  [Tailor] Bullets reordered ({reordered_count} swaps across {len(entries)} entries)")


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_key(master_path: Path, jd_text: str) -> str:
    payload = str(master_path) + jd_text[:600]
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cached_path(master_path: Path, jd_text: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key  = _cache_key(master_path, jd_text)
    stem = master_path.stem
    return CACHE_DIR / f"{stem}_tailored_{key}.docx"


# ── Main entry point ──────────────────────────────────────────────────────────

def tailor_resume(master_path: Path, jd_text: str, force: bool = False) -> Path:
    """
    Tailor master_path DOCX to jd_text. Returns path to tailored DOCX.
    Results are cached — same master + same JD always returns the same file.
    Set force=True to bypass cache and re-tailor.
    """
    out_path = _cached_path(master_path, jd_text)

    if out_path.exists() and not force:
        print(f"  [Tailor] Cache hit → {out_path.name}")
        return out_path

    if master_path.suffix.lower() != ".docx":
        print(f"  [Tailor] {master_path.name} is not DOCX — skipping tailor")
        return master_path

    print(f"  [Tailor] Tailoring {master_path.name} …")
    shutil.copy2(master_path, out_path)

    doc       = Document(out_path)
    jd        = extract_jd_keywords(jd_text)
    jd_words  = jd["raw_words"]
    structure = parse_resume(doc)

    _tailor_title(doc, structure, jd)
    _tailor_summary(doc, structure, jd)

    if structure["skills_table_idx"] is not None:
        _tailor_skills_table(doc, structure["skills_table_idx"], jd_words)
    elif structure["skills_para_idxs"]:
        _tailor_skills_paras(doc, structure["skills_para_idxs"], jd_words)

    _tailor_bullets(doc, structure["experience_entries"], jd_words)

    doc.save(out_path)
    print(f"  [Tailor] Saved → {out_path.name}")
    return out_path


def clear_cache():
    """Remove all cached tailored resumes."""
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)
        print(f"Cache cleared: {CACHE_DIR}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Tailor a DOCX resume to a job description")
    parser.add_argument("master",  help="Path to master DOCX resume")
    parser.add_argument("jd",      help="JD text or path to a .txt/.md file containing JD")
    parser.add_argument("--output", help="Output DOCX path (default: auto-named in cache dir)")
    parser.add_argument("--force",  action="store_true", help="Ignore cache and re-tailor")
    args = parser.parse_args()

    master = Path(args.master)
    if not master.exists():
        print(f"ERROR: {master} not found"); sys.exit(1)

    jd_src = Path(args.jd)
    if jd_src.exists():
        jd_text = jd_src.read_text(encoding="utf-8")
    else:
        jd_text = args.jd

    result = tailor_resume(master, jd_text, force=args.force)

    if args.output:
        shutil.copy2(result, args.output)
        print(f"Copied to {args.output}")
    else:
        print(f"Output: {result}")
