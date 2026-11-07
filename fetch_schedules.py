
"""Independent CNATRA multi-squadron schedule downloader."""

import argparse
import hashlib
import io
import json
import re
import socket
import ssl
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import certifi
import pdfplumber
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "schedules"
HOST = "www.cnatra.navy.mil"
TZ = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")

# The scheduled workflow monitors 1 PM through 8 PM Central.
MONITOR_START = 13
MONITOR_END = 20

# Today and seven additional calendar days.
LOOKAHEAD_DAYS = 7

# Distant dates are checked once per hour, near the beginning
# of the hour. Near-term dates are checked on every run.
DISTANT_CHECK_MINUTE = 18


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def trust_bundle(folder):
    """Retrieve the issuing CA and use verified HTTPS."""

    context = ssl._create_unverified_context()

    with socket.create_connection((HOST, 443), timeout=20) as sock:
        with context.wrap_socket(sock, server_hostname=HOST) as tls:
            leaf = x509.load_der_x509_certificate(
                tls.getpeercert(binary_form=True)
            )

    aia = leaf.extensions.get_extension_for_class(
        x509.AuthorityInformationAccess
    ).value

    for entry in aia:
        if entry.access_method != AuthorityInformationAccessOID.CA_ISSUERS:
            continue

        url = entry.access_location.value

        if urlparse(url).scheme not in ("http", "https"):
            continue

        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            data = response.content

            try:
                certs = [x509.load_der_x509_certificate(data)]
            except ValueError:
                try:
                    certs = [x509.load_pem_x509_certificate(data)]
                except ValueError:
                    certs = pkcs7.load_der_pkcs7_certificates(data)

            for cert in certs:
                if cert.subject == leaf.issuer:
                    path = Path(folder) / "trust.pem"
                    path.write_bytes(
                        Path(certifi.where()).read_bytes()
                        + b"\n"
                        + cert.public_bytes(
                            serialization.Encoding.PEM
                        )
                    )
                    return str(path)

        except (requests.RequestException, ValueError):
            continue

    raise RuntimeError("Could not verify CNATRA certificate chain")


def regular_squadron(identifier):
    return bool(re.fullmatch(r"(VT|HT)-\d+", identifier))


def schedule_url(identifier, info, day):
    """Use configured URL or standard CNATRA URL pattern."""

    explicit = info.get("url_template")

    if explicit:
        return explicit.replace("{date}", day)

    if not regular_squadron(identifier):
        return None

    filename = f"!{day}!{identifier}!Frontpage.pdf"

    return (
        f"https://{HOST}/scheds/"
        f"{info['wing']}/SQ-{identifier}/"
        f"{quote(filename, safe='!')}"
    )


def extract_pdf(data):
    pages = []
    tables = []
    records = []
    text_parts = []

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""

            pages.append({
                "page": number,
                "text": text
            })

            text_parts.append(
                f"===== PAGE {number} =====\n{text}"
            )

            for line_number, line in enumerate(
                text.splitlines(), 1
            ):
                if normalize(line):
                    records.append({
                        "page": number,
                        "line_number": line_number,
                        "text": normalize(line)
                    })

            for table_number, table in enumerate(
                page.extract_tables(), 1
            ):
                rows = []

                for row in table:
                    if row:
                        cells = [normalize(c) for c in row]

                        if any(cells):
                            rows.append(cells)

                if rows:
                    tables.append({
                        "page": number,
                        "table_number": table_number,
                        "rows": rows
                    })

    if not records:
        raise ValueError("PDF has no extractable text")

    return {
        "pages": pages,
        "tables": tables,
        "records": records
    }, "\n\n".join(text_parts)


def matches_person(text, name):
    if not name:
        return False

    parts = normalize(name).upper().split()

    if len(parts) < 2:
        return False

    first, last = parts[0], parts[-1]

    variants = [
        f"{first} {last}",
        f"{last} {first}",
        f"{last}, {first}",
        f"{last} {first[0]}",
        f"{last}, {first[0]}"
    ]

    text = normalize(text).upper()

    return any(
        re.search(
            r"(?<![A-Z])" + re.escape(v) + r"(?![A-Z])",
            text
        )
        for v in variants
    )


def search_records(parsed, name):
    matches = []

    if not name:
        return matches

    for record in parsed["records"]:
        if matches_person(record["text"], name):
            matches.append({
                "source": "text",
                **record
            })

    for table in parsed["tables"]:
        for index, row in enumerate(table["rows"], 1):
            if any(matches_person(c, name) for c in row):
                matches.append({
                    "source": "table",
                    "page": table["page"],
                    "table_number": table["table_number"],
                    "row_number": index,
                    "cells": row
                })

    return matches


def file_hash(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )


def next_flying_day(today):
    """Return next weekday, skipping weekends."""

    day = today + timedelta(days=1)

    while day.weekday() >= 5:
        day += timedelta(days=1)

    return day


def target_dates(now, override):
    """Today plus seven days, or one explicitly selected date."""

    if override:
        return [
            datetime.strptime(
                override, "%Y-%m-%d"
            ).date().isoformat()
        ]

    return [
        (now.date() + timedelta(days=n)).isoformat()
        for n in range(LOOKAHEAD_DAYS + 1)
    ]


def should_check_date(now, day, override):
    """
    Check today, tomorrow, and the next flying day every run.
    Check more distant dates once per hour.

    The next flying day includes Monday when today is Friday.
    """

    if override:
        return True

    target = datetime.strptime(
        day, "%Y-%m-%d"
    ).date()

    distance = (target - now.date()).days

    if distance <= 1:
        return True

    if target == next_flying_day(now.date()):
        return True

    return now.minute < DISTANT_CHECK_MINUTE


def check_schedule(
    session,
    verify,
    identifier,
    info,
    day,
    name,
    force
):
    url = schedule_url(identifier, info, day)

    if url is None:
        return "endpoint_unconfigured"

    parsed_url = urlparse(url)

    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != HOST
    ):
        return "invalid_endpoint"

    folder = OUTPUT / identifier / day
    pdf_path = folder / "original_schedule.pdf"
    json_path = folder / "full_schedule.json"
    status_path = folder / "status.json"

    # Do not trust a previously downloaded status as proof
    # that CNATRA has not published a revised schedule.
    # Every selected date is checked again.

    try:
        response = session.get(
            url,
            timeout=(15, 45),
            verify=verify
        )

        if response.status_code in (404, 410):
            return "not_published"

        response.raise_for_status()

        data = response.content

        if not data.startswith(b"%PDF-"):
            return "invalid_pdf_response"

    except requests.RequestException as exc:
        print(
            f"ERROR {identifier} {day}: "
            f"{type(exc).__name__}: {exc}"
        )
        return "retry_needed"

    new_hash = file_hash(data)

    # An unchanged PDF needs no parsing or repository commit.
    # Force bypasses this optimization.
    if not force and pdf_path.exists() and json_path.exists():
        try:
            old_hash = file_hash(pdf_path.read_bytes())

            if old_hash == new_hash:
                return "unchanged"

        except OSError:
            pass

    previous_exists = pdf_path.exists()

    try:
        parsed, full_text = extract_pdf(data)

    except (ValueError, OSError, Exception) as exc:
        print(
            f"ERROR parsing {identifier} {day}: "
            f"{type(exc).__name__}: {exc}"
        )
        return "retry_needed"

    metadata = {
        "schedule_id": identifier,
        "schedule_name": info.get("name", identifier),
        "wing": info["wing"],
        "schedule_date": day,
        "source_url": url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "pages_read": len(parsed["pages"]),
        "table_count": len(parsed["tables"]),
        "text_record_count": len(parsed["records"]),
        "pdf_sha256": new_hash
    }

    folder.mkdir(parents=True, exist_ok=True)

    pdf_path.write_bytes(data)

    write_json(
        json_path,
        {"metadata": metadata, **parsed}
    )

    (folder / "full_schedule.txt").write_text(
        full_text,
        encoding="utf-8"
    )

    if name:
        matches = search_records(parsed, name)

        write_json(
            folder / "schedule_results.json",
            {
                "metadata": metadata,
                "search_name": name,
                "match_count": len(matches),
                "matches": matches
            }
        )

    write_json(
        status_path,
        {
            "status": "downloaded",
            **metadata
        }
    )

    if previous_exists:
        return "revised"

    return "downloaded"


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--squadron",
        default="all"
    )

    parser.add_argument(
        "--date",
        default=""
    )

    parser.add_argument(
        "--name",
        default=""
    )

    parser.add_argument(
        "--force",
        action="store_true"
    )

    args = parser.parse_args()

    config = json.loads(
        (ROOT / "squadrons.json").read_text(
            encoding="utf-8"
        )
    )

    squadrons = config["squadrons"]

    if args.squadron != "all":
        if args.squadron not in squadrons:
            parser.error("Unknown schedule identifier")

        selected = {
            args.squadron: squadrons[args.squadron]
        }
    else:
        selected = squadrons

    now = datetime.now(TZ)

    print("=" * 60)
    print("FLIGHTLINE CNATRA SCHEDULE CHECK")
    print("=" * 60)
    print(f"Central time: {now.isoformat()}")
    print(f"UTC time: {datetime.now(UTC).isoformat()}")
    print(f"Squadrons selected: {len(selected)}")
    print(f"Manual date: {args.date or 'automatic'}")
    print(f"Force reprocess: {args.force}")

    if not args.date:
        if (
            now.weekday() >= 5
            or not MONITOR_START <= now.hour < MONITOR_END
        ):
            print("Outside scheduled monitoring window")
            return

    dates = target_dates(now, args.date)

    selected_dates = [
        day for day in dates
        if should_check_date(now, day, args.date)
    ]

    print(f"Dates in lookahead: {', '.join(dates)}")
    print(f"Dates checked this run: {', '.join(selected_dates)}")

    if not args.date:
        print(
            "Next flying day: "
            f"{next_flying_day(now.date()).isoformat()}"
        )

    print(
        "Maximum squadron-date checks: "
        f"{len(selected) * len(selected_dates)}"
    )

    outcomes = {}
    counts = Counter()

    with tempfile.TemporaryDirectory() as temp:
        try:
            verify = trust_bundle(temp)

        except Exception as exc:
            print(
                "HTTPS trust setup failed: "
                f"{type(exc).__name__}: {exc}"
            )
            raise SystemExit(1)

        with requests.Session() as session:
            session.headers.update({
                "User-Agent": "FlightLine-CNATRA-Schedule-Checker/1.0",
                "Cache-Control": "no-cache"
            })

            for identifier, info in selected.items():
                for day in selected_dates:
                    key = f"{identifier}/{day}"

                    try:
                        result = check_schedule(
                            session,
                            verify,
                            identifier,
                            info,
                            day,
                            args.name,
                            args.force
                        )

                    except Exception as exc:
                        print(
                            f"ERROR {key}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        result = "retry_needed"

                    outcomes[key] = result
                    counts[result] += 1

                    print(f"{key}: {result}")

    print("\n" + "=" * 60)
    print("SCHEDULE CHECK SUMMARY")
    print("=" * 60)

    for status, count in sorted(counts.items()):
        print(f"{status}: {count}")

    print(f"Total checked: {len(outcomes)}")
    print(f"New schedules: {counts['downloaded']}")
    print(f"Revised schedules: {counts['revised']}")
    print(f"Unchanged schedules: {counts['unchanged']}")
    print(f"Not published: {counts['not_published']}")
    print(f"Retries needed: {counts['retry_needed']}")

    print("\nDetailed results:")
    print(json.dumps(outcomes, indent=2))


if __name__ == "__main__":
    main()
