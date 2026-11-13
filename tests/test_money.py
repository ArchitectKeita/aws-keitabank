import os, sys, json, importlib, threading
os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_REGION="us-east-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                  TABLE_NAME="keitabank", CLIENT_ID="abc123", AUTH_DOMAIN="d.auth.us-east-1.amazoncognito.com")
from moto import mock_aws
import boto3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "api"))
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
n = [0]
def key(): n[0] += 1; return f"test-key-{n[0]:04d}"

with mock_aws():
    boto3.client("dynamodb").create_table(TableName="keitabank",
        KeySchema=[{"AttributeName":"PK","KeyType":"HASH"},{"AttributeName":"SK","KeyType":"RANGE"}],
        AttributeDefinitions=[{"AttributeName":"PK","AttributeType":"S"},{"AttributeName":"SK","AttributeType":"S"}],
        BillingMode="PAY_PER_REQUEST")
    app = importlib.import_module("app")
    def call(route, sub, body=None, id=None):
        ev = {"routeKey": route, "body": json.dumps(body) if body is not None else None,
              "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "email": f"{sub}@x.com"}}}}}
        if id: ev["pathParameters"] = {"id": id}
        r = app.lambda_handler(ev, None); return r["statusCode"], json.loads(r["body"])
    def bal(sub):
        return {a["nickname"]: a["balance_cents"] for a in call("GET /accounts", sub)[1]["accounts"]}

    _, me = call("GET /me", "A"); chk = me["accounts"][0]["acct_id"]
    ok(len(me["activity"]) == 1 and me["activity"][0]["title"] == "Welcome deposit", "welcome deposit is the first ledger line")
    _, sav = call("POST /accounts", "A", {"type": "savings", "nickname": "Corvette Fund"}); sav = sav["acct_id"]
    _, meB = call("GET /me", "B"); chkB = meB["accounts"][0]; chkB_num = chkB["number"]

    print("--- transfers ---")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 25000, "idempotency_key": key()})
    ok(c == 201 and bal("A") == {"Everyday Checking": 75000, "Corvette Fund": 25000}, "move $250 checking -> savings")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_number": chkB_num, "amount_cents": 10050, "memo": "for the tickets", "idempotency_key": key()})
    ok(c == 201 and bal("A")["Everyday Checking"] == 64950 and bal("B")["Everyday Checking"] == 110050, "send $100.50 to user B by account number")
    _, actB = call("GET /activity", "B")
    ok(actB["activity"][0]["amount_cents"] == 10050 and actB["activity"][0]["title"] == "for the tickets", "B's ledger shows the incoming $100.50 with memo")

    print("--- money can't be created or lost ---")
    total_before = sum(bal("A").values()) + sum(bal("B").values())
    k = key()
    c1, r1 = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 1000, "idempotency_key": k})
    c2, r2 = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 1000, "idempotency_key": k})
    ok(r2.get("replayed") and r1["txn_id"] == r2["txn_id"] and bal("A")["Everyday Checking"] == 63950, "same request twice (double-tap) moves the money ONCE")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 99999999, "idempotency_key": key()})
    ok(c == 400 and "limit" in r["error"], "over $100k limit refused")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 9000000, "idempotency_key": key()})
    ok(c == 400 and r["error"] == "insufficient funds" and bal("A")["Everyday Checking"] == 63950, "overdraft refused, balance untouched")
    for bad, why in [(-500, "negative"), (0, "zero"), (10.5, "float"), ("100", "string"), (True, "boolean")]:
        c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": bad, "idempotency_key": key()})
        ok(c == 400, f"{why} amount refused")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": chk, "amount_cents": 100, "idempotency_key": key()})
    ok(c == 400, "transfer to the same account refused")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 100})
    ok(c == 400 and "idempotency" in r["error"], "missing idempotency key refused")

    print("--- you can't touch someone else's money ---")
    c, r = call("POST /transfers", "B", {"from_acct_id": chk, "to_number": chkB_num, "amount_cents": 100, "idempotency_key": key()})
    ok(c == 404 and bal("A")["Everyday Checking"] == 63950, "B using A's account id as the SOURCE -> 404, A untouched")
    c, r = call("POST /transfers", "B", {"from_acct_id": chkB["acct_id"], "to_acct_id": chk, "amount_cents": 100, "idempotency_key": key()})
    ok(c == 404, "B can't name A's account by internal id (only by account number)")
    c, r = call("PATCH /accounts/{id}", "B", {"goal_cents": 1}, id=sav)
    ok(c == 404, "B can't edit A's account")
    c, r = call("POST /transfers", "A", {"from_acct_id": chk, "to_number": "0000000001", "amount_cents": 100, "idempotency_key": key()})
    ok(c == 400 or c == 404, "bad payee number refused")

    print("--- payments + goals ---")
    c, r = call("POST /payments", "A", {"from_acct_id": chk, "merchant": "wawa", "amount_cents": 4210, "idempotency_key": key()})
    ok(c == 201 and r["category"] == "gas" and bal("A")["Everyday Checking"] == 59740, "pay $42.10 at Wawa, tagged gas")
    c, r = call("POST /payments", "A", {"from_acct_id": chk, "merchant": "hacker", "amount_cents": 1, "idempotency_key": key()})
    ok(c == 400, "unknown merchant refused")
    c, r = call("PATCH /accounts/{id}", "A", {"goal_cents": 7000000}, id=sav)
    ok(c == 200 and r["goal_cents"] == 7000000, "Corvette Fund goal set to $70,000")

    print("--- race: 5 transfers of $200 fire at once from a $597.40 account ---")
    before = bal("A")["Everyday Checking"]
    results = []
    def go():
        results.append(call("POST /transfers", "A", {"from_acct_id": chk, "to_acct_id": sav, "amount_cents": 20000, "idempotency_key": key()}))
    ts = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    wins = sum(1 for c, r in results if c == 201)
    after = bal("A")
    ok(after["Everyday Checking"] == before - wins * 20000 and after["Everyday Checking"] >= 0, f"{wins} succeeded, balance {after['Everyday Checking']/100:.2f}, never negative")
    ok(wins <= before // 20000, f"no more transfers succeeded than the money allowed ({before//20000} max)")

    print("--- deposits ---")
    before = bal("A")["Corvette Fund"]; k = key()
    c, r = call("POST /deposits", "A", {"to_acct_id": sav, "amount_cents": 35700, "source": "paycheck", "idempotency_key": k})
    c2, r2 = call("POST /deposits", "A", {"to_acct_id": sav, "amount_cents": 35700, "source": "paycheck", "idempotency_key": k})
    ok(c == 201 and bal("A")["Corvette Fund"] == before + 35700 and r2.get("replayed"), "deposit $357.00 paycheck; sent twice, added ONCE")
    c, r = call("POST /deposits", "A", {"to_acct_id": sav, "amount_cents": 500001, "idempotency_key": key()})
    ok(c == 400, "deposit over $5,000 refused")
    c, r = call("POST /deposits", "B", {"to_acct_id": sav, "amount_cents": 100, "idempotency_key": key()})
    ok(c == 404, "B can't deposit into A's account by internal id")
    c, r = call("POST /deposits", "A", {"to_acct_id": sav, "amount_cents": 100, "source": "lottery", "idempotency_key": key()})
    ok(c == 400, "unknown deposit source refused")
    DEPOSITED = 35700
    print("--- the books balance ---")
    total_after = sum(bal("A").values()) + sum(bal("B").values())
    spent = 4210
    ok(total_after == total_before - spent + DEPOSITED, "total money in the bank only changed by the $42.10 paid out and the $357.00 deposited")
    for sub in ("A", "B"):
        acts = call("GET /activity", sub)[1]["activity"]
        for a in call("GET /accounts", sub)[1]["accounts"]:
            ledger = sum(x["amount_cents"] for x in acts if x["acct_id"] == a["acct_id"])
            ok(ledger == a["balance_cents"], f"{sub} {a['nickname']}: ledger sums to the balance ({a['balance_cents']/100:.2f})")
