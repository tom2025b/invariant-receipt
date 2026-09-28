"""The invariant summary (spec §4): two-way verification lives one level up.

    GAP FOUND         any mutation SURVIVED (checked first)
    TWO-WAY VERIFIED  two CAUGHT mutations with different mutation fingerprints
                      AND different failure signatures
    ONE-WAY           at least one CAUGHT, but no such pair
    INCOMPLETE        no CAUGHT and no SURVIVED

A CAUGHT receipt with no failure signature cannot count toward TWO-WAY: "we
could not tell how it failed" is not "it failed differently".
"""

from __future__ import annotations

from itertools import combinations

GAP_FOUND = "GAP FOUND"
TWO_WAY = "TWO-WAY VERIFIED"
ONE_WAY = "ONE-WAY"
INCOMPLETE = "INCOMPLETE"
STATUSES = (GAP_FOUND, TWO_WAY, ONE_WAY, INCOMPLETE)

EXIT_ALL_CAUGHT = 0
EXIT_SURVIVED = 1
EXIT_INVALID = 2
EXIT_UNKNOWN = 3
EXIT_REFUSED = 4


def invariant_status(receipts: list[dict]) -> str:
    verdicts = [r["verdict"] for r in receipts]
    if "SURVIVED" in verdicts:
        return GAP_FOUND
    caught = [r for r in receipts if r["verdict"] == "CAUGHT"]
    if not caught:
        return INCOMPLETE
    for a, b in combinations(caught, 2):
        fa, fb = a["mutation"].get("fingerprint"), b["mutation"].get("fingerprint")
        sa = (a["mutation"].get("run") or {}).get("failure_signature")
        sb = (b["mutation"].get("run") or {}).get("failure_signature")
        if fa and fb and sa and sb and fa != fb and sa != sb:
            return TWO_WAY
    return ONE_WAY


def exit_code(receipts: list[dict]) -> int:
    """Spec §8: SURVIVED wins over everything, then UNKNOWN, then INVALID."""
    verdicts = {r["verdict"] for r in receipts}
    if "SURVIVED" in verdicts:
        return EXIT_SURVIVED
    if "UNKNOWN" in verdicts:
        return EXIT_UNKNOWN
    if "INVALID" in verdicts:
        return EXIT_INVALID
    if not receipts:
        return EXIT_UNKNOWN  # nothing was checked; that is not "every mutation caught"
    return EXIT_ALL_CAUGHT


def pr_line(name: str, status: str, receipts: list[dict]) -> str:
    total = len(receipts)
    n = {
        v: sum(1 for r in receipts if r["verdict"] == v)
        for v in ("CAUGHT", "SURVIVED", "INVALID", "UNKNOWN")
    }
    extra = [f"{n[v]} {v.lower()}" for v in ("SURVIVED", "INVALID", "UNKNOWN") if n[v]]
    tail = f"{n['CAUGHT']}/{total} caught" + (", " + ", ".join(extra) if extra else "")
    return f"{name} — {status} ({tail})"


def build(receipts: list[dict], tool: dict) -> dict:
    """summary.json content, computed only from receipts."""
    by_inv: dict[str, list[dict]] = {}
    for r in receipts:
        by_inv.setdefault(r["invariant"], []).append(r)
    invariants = []
    for name in sorted(by_inv):
        rs = sorted(by_inv[name], key=lambda r: r["mutation"]["id"])
        status = invariant_status(rs)
        invariants.append(
            {
                "invariant": name,
                "status": status,
                "pr_line": pr_line(name, status, rs),
                "counts": {
                    v: sum(1 for r in rs if r["verdict"] == v)
                    for v in ("CAUGHT", "SURVIVED", "INVALID", "UNKNOWN")
                },
                "mutations": [
                    {
                        "id": r["mutation"]["id"],
                        "verdict": r["verdict"],
                        "reason": r["reason"],
                        "fingerprint": r["mutation"].get("fingerprint"),
                        "failure_signature": (r["mutation"].get("run") or {}).get(
                            "failure_signature"
                        ),
                        "receipt": f"{name}/{r['mutation']['id']}/receipt.json",
                    }
                    for r in rs
                ],
            }
        )
    # Most important news first: GAP FOUND leads.
    order = {s: i for i, s in enumerate(STATUSES)}
    invariants.sort(key=lambda i: (order[i["status"]], i["invariant"]))
    return {
        "schema": "invariant-receipt/summary/1",
        "tool": tool,
        "exit_code": exit_code(receipts),
        "refused": None,
        "invariants": invariants,
    }
