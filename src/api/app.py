"""
KeitaBank API -- accounts, transfers, payments, and the ledger.

Every route except GET / sits behind API Gateway's JWT authorizer, so by the
time this code runs, Cognito has already verified who the caller is. Identity
comes from the token's `sub` claim -- never from anything the client sends --
so one user can never read or move another user's money.

Rules this file enforces:
  * Money is integer CENTS. Never floats.
  * A movement of money is ONE DynamoDB transaction: both balances and both
    ledger entries change together, or nothing changes.
  * Balances use optimistic locking: the write only succeeds if the balance
    is still exactly what we read. Two transfers racing can't both spend the
    same dollar.
  * Every money movement carries an idempotency key. Sending the same request
    twice -- a double-tap, a network retry -- moves the money once.
  * Ledger entries are written once and never updated.

Single-table design (PK / SK):
  USER#<sub>    PROFILE                    email, created
  USER#<sub>    ACCT#<acct_id>             type, nickname, number, balance_cents, status, goal_cents
  USER#<sub>    TXN#<ts>#<txn_id>          one ledger line per account touched
  ACCTNUM#<n>   ACCTNUM                    owner, acct_id  (find a payee by number)
  IDEM#<sub>    <idempotency key>          the result of a request already done
"""
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

ddb = boto3.client("dynamodb")
TABLE = os.environ.get("TABLE_NAME", "keitabank")
WELCOME_CENTS = int(os.environ.get("WELCOME_CENTS", "100000"))  # $1,000.00 of fake money
MAX_ACCOUNTS = 6
MAX_MOVE_CENTS = 10_000_000  # $100,000 per movement
TYPES = {"checking", "savings"}
_deser = TypeDeserializer()

MERCHANTS = {
    "wawa":      ("Wawa", "gas"),
    "shell":     ("Shell", "gas"),
    "foodlion":  ("Food Lion", "groceries"),
    "giant":     ("Giant Food", "groceries"),
    "chipotle":  ("Chipotle", "food"),
    "chickfila": ("Chick-fil-A", "food"),
    "bestbuy":   ("Best Buy", "shopping"),
    "nike":      ("Nike", "shopping"),
    "netflix":   ("Netflix", "bills"),
    "pepco":     ("Pepco", "bills"),
    "playstation": ("PlayStation Store", "fun"),
    "amc":       ("AMC Theatres", "fun"),
}

PAGE_TEMPLATE = (Path(__file__).parent / "index.html").read_text()
PAGE = (PAGE_TEMPLATE
        .replace("__REGION__", os.environ.get("AWS_REGION", "us-east-1"))
        .replace("__CLIENT_ID__", os.environ.get("CLIENT_ID", ""))
        .replace("__AUTH_DOMAIN__", os.environ.get("AUTH_DOMAIN", "")))


class Refused(Exception):
    """A request we deliberately turn down, with a message safe to show."""
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# --- helpers -----------------------------------------------------------------------

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def plain(item):
    return {k: _deser.deserialize(v) for k, v in item.items()}


def claims(event):
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def body_of(event):
    try:
        b = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise Refused(400, "body must be JSON")
    if not isinstance(b, dict):
        raise Refused(400, "body must be a JSON object")
    return b


def cents(value):
    """Accept an integer number of cents only. Floats are refused, not rounded."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise Refused(400, "amount_cents must be a whole number of cents")
    if value <= 0:
        raise Refused(400, "amount must be more than $0.00")
    if value > MAX_MOVE_CENTS:
        raise Refused(400, "amount is over the $100,000 limit")
    return value


def idem_key(body):
    k = body.get("idempotency_key")
    if not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", k):
        raise Refused(400, "idempotency_key is required (8-64 letters, digits, - or _)")
    return k


def new_number():
    return str(secrets.randbelow(9_000_000_000) + 1_000_000_000)


def acct_view(item):
    a = plain(item)
    return {
        "acct_id": a["acct_id"], "type": a["type"], "nickname": a["nickname"],
        "number": a["number"], "number_masked": "••••" + a["number"][-4:],
        "balance_cents": int(a["balance_cents"]), "status": a["status"],
        "goal_cents": int(a.get("goal_cents", 0)), "created_at": a["created_at"],
    }


def list_accounts(sub):
    out = ddb.query(TableName=TABLE, KeyConditionExpression="PK = :p AND begins_with(SK, :a)",
                    ExpressionAttributeValues={":p": {"S": f"USER#{sub}"}, ":a": {"S": "ACCT#"}})
    accts = [acct_view(i) for i in out.get("Items", [])]
    accts.sort(key=lambda a: (a["type"] != "checking", a["created_at"]))
    return accts


def get_account(owner, acct_id):
    out = ddb.get_item(TableName=TABLE, Key={"PK": {"S": f"USER#{owner}"}, "SK": {"S": f"ACCT#{acct_id}"}},
                       ConsistentRead=True)
    return acct_view(out["Item"]) if "Item" in out else None


def account_items(sub, acct_type, nickname, balance_cents):
    acct_id = secrets.token_hex(6)
    number = new_number()
    acct = {
        "PK": {"S": f"USER#{sub}"}, "SK": {"S": f"ACCT#{acct_id}"},
        "acct_id": {"S": acct_id}, "type": {"S": acct_type}, "nickname": {"S": nickname},
        "number": {"S": number}, "balance_cents": {"N": str(balance_cents)},
        "status": {"S": "ACTIVE"}, "goal_cents": {"N": "0"}, "created_at": {"S": now()},
    }
    index = {"PK": {"S": f"ACCTNUM#{number}"}, "SK": {"S": "ACCTNUM"},
             "owner": {"S": sub}, "acct_id": {"S": acct_id}}
    return acct_id, [
        {"Put": {"TableName": TABLE, "Item": acct, "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": TABLE, "Item": index, "ConditionExpression": "attribute_not_exists(PK)"}},
    ]


def ledger_put(owner, txn_id, ts, acct_id, amount, balance_after, kind, title, category, counterparty=""):
    item = {
        "PK": {"S": f"USER#{owner}"}, "SK": {"S": f"TXN#{ts}#{txn_id}#{acct_id}"},
        "txn_id": {"S": txn_id}, "ts": {"S": ts}, "acct_id": {"S": acct_id},
        "amount_cents": {"N": str(amount)}, "balance_after_cents": {"N": str(balance_after)},
        "kind": {"S": kind}, "title": {"S": title}, "category": {"S": category},
        "counterparty": {"S": counterparty},
    }
    return {"Put": {"TableName": TABLE, "Item": item, "ConditionExpression": "attribute_not_exists(PK)"}}


def balance_update(owner, acct_id, seen, new):
    # Optimistic lock: succeed only if the balance is STILL what we read, the
    # account is active, and the result isn't negative.
    return {"Update": {
        "TableName": TABLE,
        "Key": {"PK": {"S": f"USER#{owner}"}, "SK": {"S": f"ACCT#{acct_id}"}},
        "UpdateExpression": "SET balance_cents = :new",
        "ConditionExpression": "balance_cents = :seen AND #st = :active AND :new >= :zero",
        "ExpressionAttributeNames": {"#st": "status"},
        "ExpressionAttributeValues": {":new": {"N": str(new)}, ":seen": {"N": str(seen)},
                                      ":active": {"S": "ACTIVE"}, ":zero": {"N": "0"}},
    }}


def idem_put(sub, key, result):
    return {"Put": {"TableName": TABLE, "ConditionExpression": "attribute_not_exists(PK)", "Item": {
        "PK": {"S": f"IDEM#{sub}"}, "SK": {"S": key},
        "result": {"S": json.dumps(result)}, "created_at": {"S": now()}}}}


def idem_get(sub, key):
    out = ddb.get_item(TableName=TABLE, Key={"PK": {"S": f"IDEM#{sub}"}, "SK": {"S": key}}, ConsistentRead=True)
    return json.loads(out["Item"]["result"]["S"]) if "Item" in out else None


def commit(sub, key, build):
    """Run a money movement exactly once.

    `build()` reads current balances and returns (transact_items, result).
    If the transaction fails because a balance moved underneath us, re-read and
    retry. If it fails because this idempotency key was already used, return
    the original result instead of moving the money again.
    """
    done = idem_get(sub, key)
    if done:
        return {**done, "replayed": True}
    for _ in range(4):
        items, result = build()
        items.append(idem_put(sub, key, result))
        try:
            ddb.transact_write_items(TransactItems=items)
            return result
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            if reasons and reasons[-1] == "ConditionalCheckFailed":
                done = idem_get(sub, key)
                if done:
                    return {**done, "replayed": True}
            # otherwise a balance changed between read and write -- try again
    raise Refused(409, "the account was busy, try again")


# --- routes ------------------------------------------------------------------------

def ensure_profile(sub, email):
    """First sign-in: profile, a funded checking account, and its first ledger
    line, all in ONE transaction."""
    ts = now()
    profile = {"PK": {"S": f"USER#{sub}"}, "SK": {"S": "PROFILE"},
               "email": {"S": email}, "created_at": {"S": ts}}
    acct_id, items = account_items(sub, "checking", "Everyday Checking", WELCOME_CENTS)
    items.insert(0, {"Put": {"TableName": TABLE, "Item": profile, "ConditionExpression": "attribute_not_exists(PK)"}})
    items.append(ledger_put(sub, secrets.token_hex(6), ts, acct_id, WELCOME_CENTS, WELCOME_CENTS,
                            "deposit", "Welcome deposit", "income", "KeitaBank"))
    try:
        ddb.transact_write_items(TransactItems=items)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "TransactionCanceledException":
            return False
        raise


def open_account(sub, body):
    acct_type = body.get("type")
    if acct_type not in TYPES:
        raise Refused(400, "type must be checking or savings")
    nickname = str(body.get("nickname") or ("Savings" if acct_type == "savings" else "Checking")).strip()[:40]
    if len(list_accounts(sub)) >= MAX_ACCOUNTS:
        raise Refused(400, f"limit is {MAX_ACCOUNTS} accounts")
    for _ in range(3):
        acct_id, items = account_items(sub, acct_type, nickname, 0)
        try:
            ddb.transact_write_items(TransactItems=items)
            return acct_view(items[0]["Put"]["Item"])
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
    raise Refused(503, "could not allocate an account number, try again")


def update_account(sub, acct_id, body):
    sets, vals, names = [], {}, {}
    if "nickname" in body:
        nick = str(body["nickname"]).strip()[:40]
        if not nick:
            raise Refused(400, "nickname can't be empty")
        sets.append("nickname = :n"); vals[":n"] = {"S": nick}
    if "goal_cents" in body:
        g = body["goal_cents"]
        if isinstance(g, bool) or not isinstance(g, int) or g < 0 or g > 100_000_000:
            raise Refused(400, "goal_cents must be a whole number of cents, $0 to $1,000,000")
        sets.append("goal_cents = :g"); vals[":g"] = {"N": str(g)}
    if not sets:
        raise Refused(400, "nothing to update")
    try:
        out = ddb.update_item(TableName=TABLE,
                              Key={"PK": {"S": f"USER#{sub}"}, "SK": {"S": f"ACCT#{acct_id}"}},
                              UpdateExpression="SET " + ", ".join(sets),
                              ConditionExpression="attribute_exists(PK)",
                              ExpressionAttributeValues=vals, ReturnValues="ALL_NEW",
                              **({"ExpressionAttributeNames": names} if names else {}))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Refused(404, "no such account")
        raise
    return acct_view(out["Attributes"])


def find_by_number(number):
    if not isinstance(number, str) or not re.fullmatch(r"\d{10}", number):
        raise Refused(400, "account number must be 10 digits")
    out = ddb.get_item(TableName=TABLE, Key={"PK": {"S": f"ACCTNUM#{number}"}, "SK": {"S": "ACCTNUM"}})
    if "Item" not in out:
        raise Refused(404, "no account with that number")
    i = plain(out["Item"])
    return i["owner"], i["acct_id"]


def transfer(sub, body):
    amount = cents(body.get("amount_cents"))
    key = idem_key(body)
    from_id = body.get("from_acct_id")
    memo = str(body.get("memo") or "").strip()[:60]
    if body.get("to_acct_id"):
        to_owner, to_id = sub, body["to_acct_id"]
    else:
        to_owner, to_id = find_by_number(body.get("to_number"))
    if to_owner == sub and to_id == from_id:
        raise Refused(400, "can't transfer an account to itself")

    def build():
        src = get_account(sub, from_id)
        dst = get_account(to_owner, to_id)
        if not src:
            raise Refused(404, "no such source account")
        if not dst:
            raise Refused(404, "no such destination account")
        if src["status"] != "ACTIVE" or dst["status"] != "ACTIVE":
            raise Refused(403, "account is not active")
        if src["balance_cents"] < amount:
            raise Refused(400, "insufficient funds")
        ts, txn_id = now(), secrets.token_hex(8)
        src_after, dst_after = src["balance_cents"] - amount, dst["balance_cents"] + amount
        internal = to_owner == sub
        out_title = f"To {dst['nickname']}" if internal else f"Sent to ••••{dst['number'][-4:]}"
        in_title = f"From {src['nickname']}" if internal else f"Received from ••••{src['number'][-4:]}"
        items = [
            balance_update(sub, from_id, src["balance_cents"], src_after),
            balance_update(to_owner, to_id, dst["balance_cents"], dst_after),
            ledger_put(sub, txn_id, ts, from_id, -amount, src_after, "transfer",
                       memo or out_title, "transfer" if internal else "sent", dst["number"][-4:]),
            ledger_put(to_owner, txn_id, ts, to_id, amount, dst_after, "transfer",
                       memo or in_title, "transfer" if internal else "received", src["number"][-4:]),
        ]
        return items, {"txn_id": txn_id, "amount_cents": amount, "from_balance_cents": src_after,
                       "internal": internal, "ts": ts}

    return commit(sub, key, build)


def pay(sub, body):
    amount = cents(body.get("amount_cents"))
    key = idem_key(body)
    m = MERCHANTS.get(body.get("merchant"))
    if not m:
        raise Refused(400, "unknown merchant")
    name, category = m
    from_id = body.get("from_acct_id")

    def build():
        src = get_account(sub, from_id)
        if not src:
            raise Refused(404, "no such account")
        if src["status"] != "ACTIVE":
            raise Refused(403, "account is not active")
        if src["balance_cents"] < amount:
            raise Refused(400, "insufficient funds")
        ts, txn_id = now(), secrets.token_hex(8)
        after = src["balance_cents"] - amount
        items = [
            balance_update(sub, from_id, src["balance_cents"], after),
            ledger_put(sub, txn_id, ts, from_id, -amount, after, "payment", name, category, name),
        ]
        return items, {"txn_id": txn_id, "amount_cents": amount, "from_balance_cents": after,
                       "merchant": name, "category": category, "ts": ts}

    return commit(sub, key, build)


DEPOSIT_SOURCES = {"paycheck": "Paycheck", "cash": "Cash deposit", "refund": "Refund", "gift": "Gift"}
MAX_DEPOSIT_CENTS = 500_000   # demo money, but still capped: $5,000 per deposit


def deposit(sub, body):
    """Demo-only: money arriving from outside the bank (a paycheck, cash)."""
    amount = cents(body.get("amount_cents"))
    if amount > MAX_DEPOSIT_CENTS:
        raise Refused(400, "deposits are limited to $5,000 at a time")
    key = idem_key(body)
    title = DEPOSIT_SOURCES.get(body.get("source", "paycheck"))
    if not title:
        raise Refused(400, "unknown deposit source")
    to_id = body.get("to_acct_id")

    def build():
        dst = get_account(sub, to_id)
        if not dst:
            raise Refused(404, "no such account")
        if dst["status"] != "ACTIVE":
            raise Refused(403, "account is not active")
        ts, txn_id = now(), secrets.token_hex(8)
        after = dst["balance_cents"] + amount
        items = [
            balance_update(sub, to_id, dst["balance_cents"], after),
            ledger_put(sub, txn_id, ts, to_id, amount, after, "deposit", title, "income", title),
        ]
        return items, {"txn_id": txn_id, "amount_cents": amount, "balance_cents": after, "source": title, "ts": ts}

    return commit(sub, key, build)


def activity(sub):
    out = ddb.query(TableName=TABLE, KeyConditionExpression="PK = :p AND begins_with(SK, :t)",
                    ExpressionAttributeValues={":p": {"S": f"USER#{sub}"}, ":t": {"S": "TXN#"}},
                    ScanIndexForward=False, Limit=300)
    rows = []
    for i in out.get("Items", []):
        t = plain(i)
        rows.append({"txn_id": t["txn_id"], "ts": t["ts"], "acct_id": t["acct_id"],
                     "amount_cents": int(t["amount_cents"]), "balance_after_cents": int(t["balance_after_cents"]),
                     "kind": t["kind"], "title": t["title"], "category": t["category"]})
    return rows


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    if route == "GET /":
        return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": PAGE}

    c = claims(event)
    sub = c.get("sub")
    if not sub:
        return resp(401, {"error": "not signed in"})

    try:
        if route == "GET /me":
            created = ensure_profile(sub, c.get("email", ""))
            return resp(200, {"email": c.get("email", ""), "new_customer": created,
                              "accounts": list_accounts(sub), "activity": activity(sub),
                              "merchants": {k: {"name": v[0], "category": v[1]} for k, v in MERCHANTS.items()}})
        if route == "GET /accounts":
            return resp(200, {"accounts": list_accounts(sub)})
        if route == "GET /activity":
            return resp(200, {"activity": activity(sub)})
        if route == "POST /accounts":
            return resp(201, open_account(sub, body_of(event)))
        if route == "PATCH /accounts/{id}":
            acct_id = (event.get("pathParameters") or {}).get("id", "")
            return resp(200, update_account(sub, acct_id, body_of(event)))
        if route == "POST /transfers":
            return resp(201, transfer(sub, body_of(event)))
        if route == "POST /deposits":
            return resp(201, deposit(sub, body_of(event)))
        if route == "POST /payments":
            return resp(201, pay(sub, body_of(event)))
    except Refused as r:
        return resp(r.code, {"error": r.msg})

    return resp(404, {"error": f"no route {route}"})
