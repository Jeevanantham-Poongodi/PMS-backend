"""Flow 4 — Expiry management: scan and every branch (US4, FR-024, SC-001)."""

from app.domain import expiry


def scan(env):
    return expiry.scan_cursor(env.cur, env.user, None)


def case_for(env, batch_id):
    return env.cur.execute(
        "select id from public.flow_cases where flow = 4 and (data->>'batch_id')::bigint = %s",
        (batch_id,),
    ).fetchone()["id"]


def test_scan_windows_and_no_duplicates(flow_env):
    env = flow_env
    b20 = env.stock_in("pharmacy", "para", "E20", 20, 5)
    b50 = env.stock_in("pharmacy", "para", "E50", 50, 5)
    b80 = env.stock_in("pharmacy", "para", "E80", 80, 5)
    env.stock_in("pharmacy", "para", "E200", 200, 5)
    env.stock_in("quarantine_qc", "para", "EQC", 20, 5)  # quarantine: not scanned
    created = scan(env)
    assert len(created) == 3
    windows = {b: env.case(case_for(env, b))["data"]["window_days"] for b in (b20, b50, b80)}
    assert windows == {b20: 30, b50: 60, b80: 90}
    assert scan(env) == []


def test_outside_window_goes_straight_to_records_updated(flow_env):
    env = flow_env
    batch = env.stock_in("pharmacy", "para", "FAR", 200, 5)
    case = env.create(
        4,
        "f4_scan",
        {"batch_id": batch, "location_id": env.loc["pharmacy"]},
        [("para", 5, {"batch_id": batch, "location_id": env.loc["pharmacy"]})],
    )
    env.act(case)
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    assert env.case(case)["closed"]


def scanned(env, days=20, qty=5):
    batch = env.stock_in("pharmacy", "para", f"S{days}", days, qty)
    scan(env)
    case = case_for(env, batch)
    env.act(case)  # → Within 90, 60 or 30 days?
    env.act(case, "Yes")
    return case, batch


def test_vendor_accepts_return(flow_env):
    env = flow_env
    case, batch = scanned(env)
    env.act(case, move_choice={"choice": "vendor"})
    assert env.outcomes(case) == ["Yes", "No"]
    env.act(case, "Yes")
    assert env.balance(batch, "pharmacy") == 0
    env.act(case)
    assert env.case(case)["closed"]


def test_move_to_ward_then_watch_list_then_used(flow_env):
    env = flow_env
    case, batch = scanned(env)
    env.act(case, move_choice={"choice": "ward", "location_id": env.loc["icu"]})
    assert env.balance(batch, "icu") == 5
    assert env.outcomes(case) == ["No"]
    env.act(case, "No")
    env.act(case)  # → Used before expiry date?
    assert env.outcomes(case) == []  # stock left, not yet expired: stays on the watch list
    env.cur.execute(
        "insert into public.stock_documents (hospital_id, doc_type) values (%s, 'issue')"
        " returning id",
        (env.hospital_id,),
    )
    doc = env.cur.fetchone()["id"]
    env.cur.execute(
        "insert into public.ledger_lines (hospital_id, document_id, item_id, batch_id,"
        " location_id, qty) values (%s, %s, %s, %s, %s, -5)",
        (env.hospital_id, doc, env.items["para"], batch, env.loc["icu"]),
    )
    assert env.outcomes(case) == ["Yes"]
    env.act(case, "Yes")
    assert env.case(case)["closed"]


def test_expired_quarantine_writeoff_investigate_and_dispose(flow_env):
    env = flow_env
    case, batch = scanned(env, days=0)
    env.act(case, move_choice={"choice": "vendor"})
    env.act(case, "No")
    env.act(case)
    assert env.outcomes(case) == ["No, expired"]
    env.act(case, "No, expired")
    assert env.balance(batch, "quarantine_expiry") == 5
    env.act(case)  # → Write-off approved?
    env.act(case, "Rejected", note="Count mismatch")
    env.act(case, "Resubmit")
    env.act(case, "Approved")
    env.act(case)  # Dispose → Stock records updated
    assert env.balance(batch, "quarantine_expiry") == 0
    assert env.case(case)["closed"]
