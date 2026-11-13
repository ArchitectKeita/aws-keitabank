import os, sys, json, importlib
os.environ.update(AWS_DEFAULT_REGION="us-east-1", AWS_REGION="us-east-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                  TABLE_NAME="keitabank", CLIENT_ID="abc123", AUTH_DOMAIN="keitabank-xyz.auth.us-east-1.amazoncognito.com")
from moto import mock_aws
import boto3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "api"))
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)

with mock_aws():
    boto3.client("dynamodb").create_table(TableName="keitabank",
        KeySchema=[{"AttributeName":"PK","KeyType":"HASH"},{"AttributeName":"SK","KeyType":"RANGE"}],
        AttributeDefinitions=[{"AttributeName":"PK","AttributeType":"S"},{"AttributeName":"SK","AttributeType":"S"}],
        BillingMode="PAY_PER_REQUEST")
    app = importlib.import_module("app")

    def call(route, sub=None, email="keita@example.com", body=None):
        ev = {"routeKey": route, "body": json.dumps(body) if body is not None else None, "requestContext": {}}
        if sub: ev["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": sub, "email": email}}}}
        r = app.lambda_handler(ev, None)
        try: return r["statusCode"], json.loads(r["body"])
        except Exception: return r["statusCode"], r["body"]

    c, b = call("GET /"); ok(c == 200 and "abc123" in b and "keitabank-xyz.auth" in b and "__CLIENT_ID__" not in b, "page served with Cognito config filled in")
    c, b = call("GET /me"); ok(c == 401, "no token -> 401 (defense in depth behind the authorizer)")

    c, b = call("GET /me", sub="user-A")
    ok(c == 200 and b["new_customer"] and len(b["accounts"]) == 1 and b["accounts"][0]["balance_cents"] == 100000, "first sign-in: checking opened with $1,000.00")
    ok(b["accounts"][0]["number_masked"].startswith("••••") and len(b["accounts"][0]["number_masked"]) == 8, "account number masked to last 4")
    c, b = call("GET /me", sub="user-A")
    ok(not b["new_customer"] and len(b["accounts"]) == 1, "second sign-in: no second welcome deposit")

    c, b = call("POST /accounts", sub="user-A", body={"type": "savings", "nickname": "Corvette Fund"})
    ok(c == 201 and b["balance_cents"] == 0 and b["nickname"] == "Corvette Fund", "open savings: $0, nickname kept")
    c, b = call("POST /accounts", sub="user-A", body={"type": "crypto"}); ok(c == 400, "bad account type -> 400")
    c, b = call("POST /accounts", sub="user-A", body="not an object"); ok(c == 400, "non-object body -> 400, no crash")

    # isolation: user B sees only their own
    call("GET /me", sub="user-B", email="b@example.com")
    c, a = call("GET /accounts", sub="user-A"); c, bb = call("GET /accounts", sub="user-B")
    ok(len(a["accounts"]) == 2 and len(bb["accounts"]) == 1, "user A sees 2 accounts, user B sees only their 1")
    ida = {x["acct_id"] for x in a["accounts"]}; idb = {x["acct_id"] for x in bb["accounts"]}
    ok(not (ida & idb), "no account overlap between users")
    ok("balance_cents" in a["accounts"][0] and isinstance(a["accounts"][0]["balance_cents"], int), "money is integer cents")

    for i in range(10): call("POST /accounts", sub="user-A", body={"type": "savings"})
    c, a = call("GET /accounts", sub="user-A"); ok(len(a["accounts"]) == 6, "account limit holds at 6")

    # account numbers are unique and indexed
    items = boto3.client("dynamodb").scan(TableName="keitabank")["Items"]
    nums = [i["number"]["S"] for i in items if i["SK"]["S"].startswith("ACCT#")]
    idx = [i for i in items if i["SK"]["S"] == "ACCTNUM"]
    ok(len(nums) == len(set(nums)) == len(idx), f"{len(nums)} accounts, {len(idx)} number-index rows, all unique")
    ok(all(len(n) == 10 and n[0] != "0" for n in nums), "10-digit account numbers")
