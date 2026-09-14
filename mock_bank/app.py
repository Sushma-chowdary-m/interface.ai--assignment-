from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import os
import time

app = Flask(__name__)
app.secret_key = "fake-bank-secret-key-2024"

# ── Fake member database ──────────────────────────────────────────────────────
MEMBERS = {
    "482915": {
        "id": "482915",
        "name": "John Smith",
        "email": "john.smith@email.com",
        "savings_balance": "$4,250.00",
        "checking_balance": "$1,820.50",
        "status": "active",
        "accounts": ["SAV-482915", "CHK-482915"],
    },
    "738204": {
        "id": "738204",
        "name": "Maria Garcia",
        "email": "maria.garcia@email.com",
        "savings_balance": "$12,750.00",
        "checking_balance": "$3,400.00",
        "status": "active",
        "accounts": ["SAV-738204", "CHK-738204"],
    },
    "990017": {
        "id": "990017",
        "name": "Restricted User",
        "email": "restricted@email.com",
        "savings_balance": "$0.00",
        "checking_balance": "$0.00",
        "status": "restricted",
        "accounts": [],
    },
}

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == "j.martinez" and password == "REDACTED_PASSWORD":
            session["logged_in"] = True
            session["user"] = username
            session["login_time"] = time.time()
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/dashboard")
def dashboard():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    # Simulate session timeout after 10 minutes
    login_time = session.get("login_time", time.time())
    if time.time() - login_time > 600:
        session.clear()
        return render_template("login.html", error="Session expired. Please log in again.")
    return render_template("dashboard.html", user=session.get("user"))


@app.route("/search", methods=["GET", "POST"])
def search():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    result = None
    error = None
    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()
        if member_id in MEMBERS:
            result = MEMBERS[member_id]
        else:
            error = f"No member found with ID: {member_id}"
    return render_template("search.html", result=result, error=error)


@app.route("/member/<member_id>")
def member_detail(member_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("error.html", message=f"Member {member_id} not found.")
    if member["status"] == "restricted":
        return render_template("error.html", message="Permission denied. This account is restricted.")
    return render_template("member_detail.html", member=member)


@app.route("/member/<member_id>/open-account", methods=["GET", "POST"])
def open_account(member_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("error.html", message=f"Member {member_id} not found.")
    if member["status"] == "restricted":
        return render_template("error.html", message="Permission denied. Cannot open account for restricted member.")
    
    confirmation = None
    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        initial_deposit = request.form.get("initial_deposit", "")
        # Show confirmation screen
        confirmation = {
            "account_type": account_type,
            "initial_deposit": initial_deposit,
            "member_id": member_id,
            "member_name": member["name"],
            "new_account_number": f"{account_type[:3].upper()}-{member_id}-NEW",
        }
    return render_template("open_account.html", member=member, confirmation=confirmation)


@app.route("/member/<member_id>/confirm-account", methods=["POST"])
def confirm_account(member_id):
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    member = MEMBERS.get(member_id)
    if not member:
        return render_template("error.html", message=f"Member {member_id} not found.")
    account_type = request.form.get("account_type", "")
    initial_deposit = request.form.get("initial_deposit", "")
    new_account = f"{account_type[:3].upper()}-{member_id}-NEW"
    return render_template("confirmation.html", member=member, account_type=account_type,
                           initial_deposit=initial_deposit, new_account=new_account)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/simulate/timeout")
def simulate_timeout():
    """Force a session timeout for testing."""
    session.clear()
    return render_template("login.html", error="Session expired. Please log in again.")


if __name__ == "__main__":
    app.run(debug=True, port=5001)