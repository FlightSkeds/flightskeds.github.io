"""Read named assignments from the printed columns, including wrapped crew lists."""
import io
import re
from datetime import date
from collections import Counter
from dataclasses import dataclass, field

import pdfplumber


@dataclass(frozen=True)
class Event:
    section: str
    label: str
    start: str
    end: str
    crew: tuple
    brief: str = ""
    formation: str = ""
    page: int = field(default=1, compare=False)
    line: str = field(default="", compare=False)
    raw: str = field(default="", compare=False)
    remarks: str = field(default="", compare=False)
    duration: str = field(default="", compare=False)
    device: str = field(default="", compare=False)
    instructors: tuple = field(default=())
    students: tuple = field(default=())



RANK = r"(?:ADM|CAPT|CDR|LCDR|LTJG|LT|ENS|COL|LTCOL|MAJ|(?:1ST|2ND|1|2)\s*LT|CTR|CIV|CAPTAIN|MR|MS|MRS|GYSGT|SSGT|SGT)"
SURNAME = r"[A-Z][A-Z'’.-]*(?:\s+[A-Z][A-Z'’.-]*){0,3}"
START = ("SCHD T/O", "T/O", "SCHD T/O ICAO", "START", "SIM TIME", "TIME RANGE")
EVENT = ("TYPE EVENT", "EVENT", "EVENTS", "TYPE SIM EVENT SIM", "TYPE FLIGHT EVENT FLIGHT", "SUBJECT")
PARSER_REVISION = 2
SECTION_TITLE = re.compile(r"(?:(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY) )?(FLIGHT|SIMULATOR) (?:\((?:FLIGHT|SIM) EVENT\) )?(?:EVENTS|SCHEDULE)")


def names(cell, allow_bare=False):
    cell = " ".join(cell.upper().replace("#", "").replace("’", "'").split())
    cell = re.sub(r"\b(1ST|2ND)\s+LT\b", r"\1LT", cell)
    prefix = rf"\b{RANK}\s+({SURNAME}?)\s+([A-Z])(?=\s*(?:[,;/]|$|\b{RANK}\b))"
    suffix = rf"({SURNAME}?)\s+([A-Z])\s*,\s*{RANK}\b"
    standard = rf"({SURNAME}?),\s*([A-Z][A-Z'-]*)\b"
    no_initial = rf"\b{RANK}\s+({SURNAME}?)(?=\s*(?:[,;/]|$|\b{RANK}\b))"
    pattern = rf"(?:{prefix})|(?:{suffix})|(?:{standard})"
    if allow_bare:
        pattern += rf"|(?:({SURNAME}?)\s+([A-Z])(?=\s*[,;/]|\s*$))"
    result = []
    for found in re.finditer(pattern, cell.strip(" ,")):
        last, first = (v for v in found.groups() if v is not None)
        if first not in {"ALL", "PERSONNEL"} and not re.fullmatch(RANK, first):
            result.append((last, first))
    if allow_bare and not result:
        result.extend((m.group(1), "") for m in re.finditer(no_initial, cell.strip(" ,")))
    return tuple(dict.fromkeys(result))


def layout(headers):
    if "ASSIGNED" in headers and any(h in headers for h in ("SIGN IN", "START")):
        return "Duty"
    if headers[:3] == ["EVENT", "DATE", "TIME"] and "ROSTER" in headers:
        return "Roster"
    if (headers[:3] == ["TIME", "EVENT", "LOCATION"] and
            any(h in headers for h in ("PERSON", "INSTRUCTOR", "STUDENT"))):
        return "Ground"
    if ("SUBJECT" in headers and "TIME RANGE" in headers) or (headers[:3] == ["TIME", "EVENT", "LOCATION"] and "REMARKS" in headers):
        return "Ground"
    if any(h in EVENT for h in headers) and any(h in START for h in headers):
        if any("SIM" in h or "DEVICE" in h for h in headers) or "START" in headers:
            return "Simulator"
        if any("LAND" in h or h == "TIME RANGE" for h in headers):
            return "Flight"
        if "SCHD T/O" in headers:
            return "Simulator"
    return None


def assignment(kind, cells, headers, page=1):
    def get(*options):
        return next((cells[i] for i, h in enumerate(headers) if h in options), "")

    def people(headers_match, bare_names=False):
        return tuple(dict.fromkeys(person for header, cell in zip(headers, cells)
                                   if headers_match(header) for person in names(cell, bare_names)))

    def times(value):
        return re.findall(r"(?<!\d)(?:[01]\d|2[0-3])[:.]?[0-5]\d(?!\d)|(?<!\d)2400(?!\d)",
                          value.replace(":", ""))

    kind_out = "Ground" if kind == "Roster" else kind
    instructors = people(lambda h: bool(re.search(r"INSTRUCTOR|MSN CDR|FRONT SEAT|RED KNIGHT|PIC", h)), True)
    students = people(lambda h: bool(re.search(r"STUDENT|BACK SEAT|SQUIRE|COPILOT", h)), True)
    if kind == "Duty":
        label = get("DUTY/WATCH", "DUTY", "WATCHBILL") or next((c for c in cells if c), kind)
        crew = names(get("ASSIGNED"), True)
        start, end = times(get("SIGN IN", "START")), times(get("SIGN OUT", "END"))
        brief = ""
    elif kind in {"Ground", "Roster"}:
        label = get(*EVENT) or kind_out
        clock = times(get("TIME", "TIME RANGE", "START"))
        start, end = clock[:1], clock[1:2] or times(get("END"))
        brief = ""
        crew = tuple(dict.fromkeys(people(lambda h: h in ("PERSON", "ROSTER"), True) + instructors + students))
    else:
        label = get(*EVENT)
        clock = times(get("TIME RANGE"))
        start = clock[1:2] or times(get(*START))
        end = clock[2:3] or times(get("SCHD LAND", "SCHED LAND", "LAND", "SCHD LAND ICAO"))
        brief_times = clock[:1] or times(get("BRIEF TIME", "BRIEF"))
        brief = next(iter(brief_times), "")
        crew = tuple(dict.fromkeys(instructors + students))
    # Some PDFs spell event codes with spaces between printed glyphs.
    compact = re.sub(r"\s+", "", label)
    if re.fullmatch(r"[A-Z]{1,5}\d{4,5}[A-Z0-9]{0,3}", compact.upper()):
        label = compact.upper()
    remarks = next((c for h, c in zip(headers, cells) if "REMARKS" in h), "")
    partner = re.search(r"\b(?:LD VS|WG VS|FORM(?:ATION)? PARTNER IS)\s+([A-Z][A-Z'-]*)", remarks.upper())
    raw = " · ".join(c for c in cells if c)
    duration = get("SCHD FLT TIME", "SCHD/ ACT FLT TIME", "SCHD/ACT FLT TIME", "SCHD TIME", "FLT TIME", "FLT HRS", "SIM HRS", "HRS", "ACTUAL TIME")
    line = get("#", "LINE", "LINE NUM", "LINE#")
    return Event(kind_out, label or kind_out, next(iter(start), ""), next(iter(end), ""), crew,
                 brief, partner[0] if partner else "", page, line, raw, remarks, duration,
                 get("SIM/DEVICE", "SIM/ DEVICE", "DEVICE/SEAT", "DEVICE", "SIM"), instructors, students)


def text_header(words):
    """Corpus Christi free-form sheets print the column labels without ruled tables."""
    labels = [word["text"].upper() for word in words]
    if "INSTRUCTOR" not in labels or "STUDENT" not in labels:
        return None
    if {"BRIEF", "T/O", "LAND", "SIM/DEVICE", "EVENT"}.issubset(labels):
        columns = [(label, word["x0"]) for label, word in zip(labels, words)
                   if label in {"BRIEF", "T/O", "LAND", "SIM/DEVICE", "INSTRUCTOR", "STUDENT", "EVENT", "HRS", "REMARKS"}]
        return [("SIM HRS" if label == "HRS" else label, x) for label, x in columns]
    if "BRF" in labels and "TO" in labels and "LAND" in labels and any(h in EVENT for h in labels):
        start = labels.index("BRF")
    elif "START" in labels and "END" in labels and "SUBJECT" in labels:
        start = labels.index("START")
    else:
        return None
    columns = [("TIME RANGE", words[start]["x0"])]
    for word, label in zip(words[start + 1:], labels[start + 1:]):
        if label in ("INSTRUCTOR", "STUDENT", "EVENT", "EVENTS", "HRS", "REMARKS", "SUBJECT", "ROOM"):
            columns.append((label, word["x0"]))
    return columns

def read_schedule(pdf, schedule_date=None):
    events, text, sections = [], [], set()
    weekday = date.fromisoformat(schedule_date).strftime("%A").upper() if schedule_date else None
    def record(kind, cells, labels, page):
        if kind == "Roster" and schedule_date:
            day = date.fromisoformat(schedule_date)
            printed = re.sub(r"[^A-Z0-9]", "", cells[labels.index("DATE")].upper())
            if printed not in {day.strftime(fmt).upper() for fmt in ("%d%b%y", "%d%b%Y", "%m%d%Y", "%m%d%y", "%Y%m%d")}:
                return
        events.append(assignment(kind, cells, labels, page))

    active = None
    unassigned = []
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        corpus_kind = "Flight"
        corpus_day = None
        seen_pages = set()
        for page in document.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                key = (page.width, page.height, page_text)
                if key in seen_pages:
                    continue
                seen_pages.add(key)
            text.append(page_text)
            markers = {}
            for table in page.find_tables():
                for header, cells in zip(table.rows, table.extract()):
                    labels = [" ".join((v or "").upper().split()) for v in cells]
                    labels = ["SIGN IN" if h == "S IGN IN" else h for h in labels]
                    kind = layout(labels)
                    if not kind:
                        continue
                    sections.add(kind)
                    markers[header.bbox[1]] = (header.bbox[3], (kind, labels, header.cells))
                    if kind in ("Duty", "Roster"):
                        markers[table.bbox[3]] = (table.bbox[3], None)
            words = page.extract_words()
            by_line = {}
            baseline = -10
            for word in sorted(words, key=lambda w: w["top"]):
                y = round(word["top"])
                if y - baseline > 2:
                    baseline = y
                by_line.setdefault(baseline, []).append(word)
            for top, line in sorted(by_line.items()):
                ordered = sorted(line, key=lambda w: w["x0"])
                line_text = " ".join(w["text"].upper() for w in ordered)
                title = SECTION_TITLE.fullmatch(line_text)
                if title:
                    corpus_kind = title[2].title()
                    if title[1]:
                        corpus_day = title[1]
                if line_text == "ACADEMIC EVENTS":
                    markers.setdefault(top, (top, None))
                    corpus_day = None
                    # VT-4's unruled academics block uses fixed columns in a
                    # 612-point wide sheet, following the DEVICE/SEAT simulator block.
                    previous = next((entry for y, (_, entry) in sorted(markers.items(), reverse=True)
                                     if y < top and entry), active)
                    if previous and "DEVICE/SEAT" in previous[1] and abs(page.width - 612) < 1:
                        labels = ["START", "END", "EVENT", "LOCATION", "INSTRUCTOR", "REMARKS"]
                        edges = [28, 62, 102, 164, 241, 349, page.width]
                        bounds = [(x, top, end, top) for x, end in zip(edges, edges[1:])]
                        markers[top] = (top, ("Ground", labels, bounds))
                        sections.add("Ground")
                elif line_text.startswith("SUBMITTED BY:"):
                    markers.setdefault(top, (top, None))
                columns = text_header(ordered)
                if not columns:
                    continue
                labels = [label for label, _ in columns]
                kind = layout(labels)
                if not kind:
                    continue
                if kind == "Flight":
                    kind = corpus_kind
                # Numeric columns are left aligned. The printed label for a
                # crew column may be centered, so allow a short left margin.
                origins = [max(0, x - (12 if i else 4)) for i, (_, x) in enumerate(columns)]
                bounds = [(x, top, end, top + 8) for x, end in zip(origins, origins[1:] + [page.width])]
                if not any(y <= top < hi for y, (hi, _) in markers.items()):
                    wrong_day = "TIME RANGE" in labels and kind in ("Flight", "Simulator") and weekday and corpus_day and weekday != corpus_day
                    markers[top] = (top + 8, None if wrong_day else (kind, labels, bounds))
                sections.add(kind)

            begin = 0
            for boundary, (resume, following) in sorted(markers.items()) + [(page.height, (0, active))]:
                if active:
                    kind, labels, bounds = active
                    current = None
                    primary = None
                    for y in sorted(by_line):
                        if not begin <= y < boundary:
                            continue
                        row = [[] for _ in bounds]
                        for word in sorted(by_line[y], key=lambda w: w["x0"]):
                            for i, column in enumerate(bounds):
                                if column and column[0]-1.5 <= word["x0"] < column[2]-1.5:
                                    row[i].append(word["text"])
                                    break
                        cells = [" ".join(col) for col in row]
                        starts = any(
                            re.match(r"^\s*\d{2}:?\d{2}\b", value)
                            for h, value in zip(labels, cells)
                            if h in START or h in ("SIGN IN", "TIME")
                        )
                        if kind == "Ground" and labels[0] == "TIME":
                            starts = bool(re.match(r"^\d{4}\s*[-–]\s*\d{4}\b", cells[0]))
                        if kind == "Roster":
                            starts = bool(cells[0] and cells[1] and cells[2])
                        if starts:
                            if current:
                                record(kind, current, labels, page.page_number)
                            current = cells
                            primary = cells
                        elif current and kind in ("Flight", "Simulator"):
                            code = next((cell for h, cell in zip(labels, cells) if h in EVENT), "")
                            separate_student = any(names(c, True) for h, c in zip(labels, cells)
                                                   if re.search(r"STUDENT|BACK SEAT|SQUIRE", h))
                            multiple_crews = "TIME RANGE" in labels or any(
                                "ICAO" in label or label == "SCHD FLT TIME" for label in labels
                            )
                            if multiple_crews and re.search(r"[A-Z]\s*\d[\d ]{2,}", code.upper()) and separate_student:
                                record(kind, current, labels, page.page_number)
                                shared = set(START) | {"BRIEF TIME", "BRIEF", "LINE NUM", "LINE", "#",
                                                       "LAND", "SCHD LAND", "SCHD LAND ICAO", "SCHED LAND",
                                                       "INSTRUCTOR", "INSTRUCTOR / PIC", "FRONT SEAT",
                                                       "RED KNIGHT", "DEVICE", "SIM/DEVICE", "SIM/ DEVICE"}
                                current = [c or (base if h in shared else "") for h, c, base in zip(labels, cells, primary)]
                            elif any(cells):
                                current = [" ".join(filter(None, pair)) for pair in zip(current, cells)]
                        elif current and any(cells) and (not cells[0] or kind == "Duty"):
                            current = [" ".join(filter(None, pair)) for pair in zip(current, cells)]
                        elif current and any(cells):
                            record(kind, current, labels, page.page_number)
                            current = None
                        if not starts and current is None and any("," in c for c in cells):
                            unassigned.append(" ".join(cells))
                    if current:
                        record(kind, current, labels, page.page_number)
                if boundary < page.height:
                    begin, active = resume, following
    raw = "\n".join(text).upper()
    totals = dict(re.findall(r"TOTAL\s+(FLY|SIM)\s+EVENTS\s*:\s*(\d+)", raw))
    counts = Counter(e.section for e in events)
    matches_totals = all(counts["Flight" if key == "FLY" else "Simulator"] == int(value)
                         for key, value in totals.items())
    complete = (len(totals) == 2 and matches_totals and
                (not re.search(r"^GROUND EVENTS\s*$", raw, re.M) or "Ground" in sections
                 or "NO GROUND EVENTS SCHEDULED" in raw) and
                ("DUTY/WATCH" not in raw or "Duty" in sections))
    if any(e.section in ("Flight", "Simulator") and not e.crew for e in events):
        complete = False
    return events, raw, complete, " ".join(unassigned)


def schedule_assignments(pdf, schedule_date=None):
    events, raw, complete, uncertain = read_schedule(pdf, schedule_date)
    return {
        "version": 1, "parser_revision": PARSER_REVISION, "complete": complete,
        "unreadable": not re.search(r"[A-Z]{3}", re.sub(r"\(CID:\d+\)", "", raw)),
        "uncertain": uncertain,
        "events": [
            {"section": e.section, "code": e.label, "start": e.start,
             "end": e.end, "land": e.end, "brief": e.brief, "formation": e.formation,
             "crew": [{"last": last, "first": first, "full": f"{last}, {first}" if first else last} for last, first in e.crew],
             "instructors": [f"{last}, {first}" if first else last for last, first in e.instructors],
             "students": [f"{last}, {first}" if first else last for last, first in e.students],
             "page": e.page, "line": e.line, "raw": e.raw, "role": e.label,
             "remarks": e.remarks, "duration": e.duration, "device": e.device,
             "solo": bool(e.students and not e.instructors and "SOLO" in e.remarks.upper())}
            for e in events
        ],
    }


def stored_schedule(data):
    assignments = data["assignments"]
    events = [
        Event(e["section"], e["code"], e["start"], e["end"],
              tuple((p["last"], p["first"]) for p in e["crew"]), e["brief"], e["formation"],
              instructors=tuple((p["last"], p["first"]) for p in e["crew"] if p["full"] in e["instructors"]),
              students=tuple((p["last"], p["first"]) for p in e["crew"] if p["full"] in e["students"]))
        for e in assignments["events"]
    ]
    return events, "\n".join(page["text"] for page in data["pages"]).upper(), assignments["complete"], assignments["uncertain"]


def personal(schedule, first, last, known):
    events, raw, complete, uncertain = schedule
    if not last or not first or len(first) < 2:
        return None
    identity = (last, first)
    assigned = [event for event in events if identity in event.crew]
    surname = re.compile(r"(?<![A-Z'-])" + re.escape(last) + r"(?![A-Z'-])")
    initials = any((last, first[0]) in event.crew or (last, "") in event.crew for event in events)
    if initials or surname.search(uncertain) or not complete:
        return None
    if assigned:
        return assigned
    if known and not surname.search(raw):
        return []
    return None
