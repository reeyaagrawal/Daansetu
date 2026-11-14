from flask import Flask, render_template,request,redirect,url_for,session , jsonify
import mysql.connector
from mysql.connector import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from uuid import uuid4
import os
from werkzeug.utils import secure_filename
import math
import secrets
from datetime import timedelta
import hmac
import re


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024

def get_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": get_csrf_token}


def is_valid_csrf_token():
    session_token = session.get("_csrf_token")
    submitted_token = request.form.get("_csrf_token", "")

    return bool(
        session_token
        and submitted_token
        and hmac.compare_digest(session_token, submitted_token)
    )


app.secret_key = os.environ.get("DAANSETU_SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("DAANSETU_SECRET_KEY is not configured")

app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

demo_payments = {}
db = mysql.connector.connect(
    host="gateway01.ap-southeast-1.prod.aws.tidbcloud.com",
    port=4000,
    user="3U7h6xnb7v41enB.root",
    password=os.environ.get("TIDB_DB_PASSWORD"),
    database="daansetu",
    ssl_ca=os.path.join(os.path.dirname(os.path.abspath(__file__)), "isrgrootx1.pem"),
    ssl_verify_cert=True,
    ssl_verify_identity=True
)

@app.before_request
def reconnect_database():
    try:
        db.ping(reconnect=True, attempts=3, delay=2)
    except mysql.connector.Error:
        app.logger.exception("Database reconnection failed")
        raise
def is_admin_user():
    user_id = session.get("user_id")

    if not user_id:
        return False

    cursor = db.cursor()

    try:
        cursor.execute(
            "SELECT role FROM users WHERE user_id = %s",
            (user_id,)
        )

        user = cursor.fetchone()

        return bool(
            user
            and user[0]
            and str(user[0]).strip().lower() == "admin"
        )

    finally:
        cursor.close()





@app.route("/admin/contact-messages", methods=["GET", "POST"])
def admin_contact_messages():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    cursor = db.cursor(dictionary=True)

    try:
        # Mark a message as read
        if request.method == "POST":

            if not is_valid_csrf_token():
                return "Invalid CSRF token. Please refresh the page.", 400

            message_id = request.form.get("message_id", "").strip()

            if not message_id.isdigit():
                return "Invalid message ID.", 400

            cursor.execute("""
                UPDATE contact_messages
                SET status = 'Read'
                WHERE message_id = %s
            """, (int(message_id),))

            db.commit()

            return redirect(url_for("admin_contact_messages"))

        # Fetch all contact messages
        cursor.execute("""
            SELECT *
            FROM contact_messages
            ORDER BY submitted_at DESC
        """)

        messages = cursor.fetchall()

    finally:
        cursor.close()

    return render_template(
        "dashboard.html",
        admin_contact_messages=True,
        messages=messages,
        is_admin=True
    )


@app.route("/")
def home():
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                COALESCE(SUM(amount), 0) AS total_donations,
                COUNT(DISTINCT user_id) AS total_donors,
                COUNT(*) AS total_completed_donations
            FROM donations
            WHERE status = 'Completed'
        """)
        stats = cursor.fetchone() or {}

        cursor.execute("""
            SELECT COUNT(*) AS total_campaigns
            FROM campaigns
        """)
        campaign_stats = cursor.fetchone() or {}

        return render_template(
            "index.html",
            total_donations=stats["total_donations"],
            total_donors=stats["total_donors"],
            total_completed_donations=stats["total_completed_donations"],
            total_campaigns=campaign_stats["total_campaigns"]
        )

    finally:
        cursor.close()



@app.route("/impact")
def impact():
    cursor = db.cursor(dictionary=True)

    try:
        # Existing impact statistics
        cursor.execute("""
            SELECT
                COALESCE(SUM(amount), 0) AS total_raised,
                COUNT(DISTINCT user_id) AS total_donors,
                COUNT(DISTINCT campaign_id) AS campaigns_supported,
                COUNT(*) AS successful_donations
            FROM donations
            WHERE status = 'Completed'
        """)

        impact_stats = cursor.fetchone()

        
        cursor.execute("SELECT COUNT(*) AS total_campaigns FROM campaigns")
        total_campaigns = cursor.fetchone()["total_campaigns"]


        
        cursor.execute("""
    SELECT COUNT(*) AS goals_achieved
    FROM (
        SELECT
            c.campaign_id,
            c.target_amount,
            COALESCE(SUM(
                CASE
                    WHEN d.status = 'Completed' THEN d.amount
                    ELSE 0
                END
            ), 0) AS raised_amount
        FROM campaigns c
        LEFT JOIN donations d
            ON c.campaign_id = d.campaign_id
        GROUP BY c.campaign_id, c.target_amount
    ) AS campaign_totals
    WHERE target_amount > 0
      AND raised_amount >= target_amount
""")

        goals_achieved = cursor.fetchone()["goals_achieved"]


        # Campaign-wise progress
        cursor.execute("""
            SELECT
                c.campaign_id,
                c.campaign_name,
                c.description,
                c.category,
                c.target_amount,
                COALESCE(SUM(
                    CASE
                        WHEN d.status = 'Completed' THEN d.amount
                        ELSE 0
                    END
                ), 0) AS raised_amount
            FROM campaigns c
            LEFT JOIN donations d
                ON c.campaign_id = d.campaign_id
            GROUP BY
                c.campaign_id,
                c.campaign_name,
                c.description,
                c.category,
                c.target_amount
            ORDER BY c.campaign_id DESC
        """)

        campaign_progress = cursor.fetchall()


        
        # Overall fundraising progress
        cursor.execute("""
            SELECT
                COALESCE(SUM(target_amount), 0) AS total_target
            FROM campaigns
        """)

        total_target = float(cursor.fetchone()["total_target"] or 0)
        total_raised = float(impact_stats["total_raised"] or 0)

        overall_progress = (
            min((total_raised / total_target) * 100, 100)
            if total_target > 0 else 0
        )


        # Calculate progress percentage safely
        for campaign in campaign_progress:
            target = float(campaign["target_amount"] or 0)
            raised = float(campaign["raised_amount"] or 0)

            campaign["progress"] = (
                min((raised / target) * 100, 100)
                if target > 0 else 0
            )

        
        
        return render_template(
    "impact.html",
    impact_stats=impact_stats,
    campaign_progress=campaign_progress,
    overall_progress=overall_progress,
    total_campaigns=total_campaigns,
    goals_achieved=goals_achieved
)



    finally:
        cursor.close()



@app.route("/contact", methods=["GET", "POST"])
def contact():

    if request.method == "POST":

        if not is_valid_csrf_token():
            return "Invalid CSRF token. Please refresh the page and try again.", 400

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()

        # Basic validation
        if not name or not email or not subject or not message:
            return render_template(
                "contact.html",
                error="Please fill in all fields."
            )

        if len(name) > 100 or len(email) > 150 or len(subject) > 200:
            return render_template(
                "contact.html",
                error="One or more fields exceed the allowed length."
            )

        if "@" not in email or "." not in email.split("@")[-1]:
            return render_template(
                "contact.html",
                error="Please enter a valid email address."
            )

        cursor = db.cursor()

        try:
            cursor.execute("""
                INSERT INTO contact_messages
                (name, email, subject, message)
                VALUES (%s, %s, %s, %s)
            """, (name, email, subject, message))

            db.commit()

            return render_template(
                "contact.html",
                success="Your message has been sent successfully!"
            )

        except mysql.connector.Error:
            db.rollback()

            return render_template(
                "contact.html",
                error="Something went wrong. Please try again."
            )

        finally:
            cursor.close()

    return render_template("contact.html")


@app.route("/donation", methods=["GET", "POST"])
def donation():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        if not is_valid_csrf_token():
            return "Invalid CSRF token. Please refresh the page and try again.", 400


        campaign_id = request.form.get("campaign", "").strip()
        amount = request.form.get("amount", "").strip()
        payment_method = request.form.get("payment", "").strip()

        # Validate campaign ID
        try:
            campaign_id = int(campaign_id)
            if campaign_id <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return "Invalid campaign selected.", 400

        # Validate donation amount
        try:
            amount = float(amount)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return "Please enter a valid donation amount.", 400

        # Validate payment method
        if payment_method not in ("upi", "card", "netbanking"):
            return "Invalid payment method.", 400

        
        # Verify campaign exists and check target status
        cursor = db.cursor()

        try:
            cursor.execute("""
                SELECT campaign_id, raised_amount, target_amount
                FROM campaigns
                WHERE campaign_id = %s
            """, (campaign_id,))

            campaign = cursor.fetchone()

        finally:
            cursor.close()

        if not campaign:
            return "Selected campaign does not exist.", 400

        if float(campaign[1]) >= float(campaign[2]):
            return render_template(
    "Donation.html",
    error="This campaign has achieved its target. Please explore other campaigns."
), 400


        

        # UPI demo payment
        if payment_method == "upi":

            payment_token = str(uuid4())

            demo_payments[payment_token] = {
                "user_id": session["user_id"],
                "campaign_id": campaign_id,
                "amount": amount,
                "status": "pending"
            }

            user_agent = request.headers.get("User-Agent", "").lower()

            is_mobile = any(
                device in user_agent
                for device in ["mobile", "android", "iphone", "ipad"]
            )

            if is_mobile:
                return redirect(
                    url_for("demo_payment", payment_token=payment_token)
                )

            return render_template(
                "Donation.html",
                show_upi=True,
                upi_amount=amount,
                upi_campaign=campaign_id,
                payment_token=payment_token
            )


        # Card / Net Banking — secure target verification
        cursor = db.cursor()

        try:
            # Lock campaign row and recheck target
            cursor.execute("""
                SELECT raised_amount, target_amount
                FROM campaigns
                WHERE campaign_id = %s
                FOR UPDATE
            """, (campaign_id,))

            campaign = cursor.fetchone()

            if not campaign:
                db.rollback()
                return "Selected campaign does not exist.", 400

            if float(campaign[0]) >= float(campaign[1]):
                db.rollback()
                return (
                    "This campaign has achieved its target. "
                    "Please explore other campaigns."
                ), 400

            # Record donation
            cursor.execute("""
                INSERT INTO donations
                (user_id, campaign_id, amount, payment_method)
                VALUES (%s, %s, %s, %s)
            """, (
                session["user_id"],
                campaign_id,
                amount,
                payment_method
            ))

            donation_id = cursor.lastrowid

            # Update raised amount
            cursor.execute("""
                UPDATE campaigns
                SET raised_amount = raised_amount + %s
                WHERE campaign_id = %s
            """, (amount, campaign_id))

            db.commit()

        except Exception:
            db.rollback()
            raise

        finally:
            cursor.close()

        session["play_donation_sound"] = True

        return redirect(
            url_for("receipt", donation_id=donation_id)
        )

    # GET request: Load campaigns for donation page
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT campaign_id, campaign_name
            FROM campaigns
            ORDER BY campaign_id DESC
        """)
        campaigns = cursor.fetchall()
    finally:
        cursor.close()

    return render_template(
        "Donation.html",
        campaigns=campaigns
    )    



# Clear temporary UPI information

@app.route("/receipt/<int:donation_id>")
def receipt(donation_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    # Consume the sound flag once, so old receipts stay silent
    play_sound = session.pop("play_donation_sound", False)

    cursor = db.cursor(dictionary=True)

    try:
        query = """
        SELECT
            d.donation_id,
            u.full_name,
            u.email,
            c.campaign_name,
            d.amount,
            d.payment_method,
            d.donation_date,
            d.status
        FROM donations d
        JOIN users u
            ON d.user_id = u.user_id
        JOIN campaigns c
            ON d.campaign_id = c.campaign_id
        WHERE d.donation_id = %s
        AND d.user_id = %s
        """

        cursor.execute(query, (donation_id, session["user_id"]))
        donation = cursor.fetchone()

    finally:
        cursor.close()

    if not donation:
        return render_template("receipt.html", error=True), 404

# Only completed donations can generate receipts
    if donation["status"] != "Completed":
        return render_template("receipt.html", error=True), 403

    return render_template(
        "receipt.html",
        donation=donation,
        play_donation_sound=play_sound
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/campaign")
def campaign():

    category = request.args.get("category", "").strip()

    cursor = db.cursor(dictionary=True)

    if category:
        cursor.execute("""
            SELECT
                campaign_id,
                campaign_name,
                description,
                target_amount,
                raised_amount,
                category,
                image_filename
            FROM campaigns
            WHERE LOWER(category) = LOWER(%s)
            ORDER BY
                CASE
                    WHEN raised_amount >= target_amount THEN 1
                    ELSE 0
                END ASC,
                campaign_id ASC
        """, (category,))
    else:
        cursor.execute("""
            SELECT
                campaign_id,
                campaign_name,
                description,
                target_amount,
                raised_amount,
                category,
                image_filename
            FROM campaigns
            ORDER BY
                CASE
                    WHEN raised_amount >= target_amount THEN 1
                    ELSE 0
                END ASC,
                campaign_id ASC
        """)

    campaigns = cursor.fetchall()
    cursor.close()

    for campaign in campaigns:
        target = float(campaign["target_amount"] or 0)
        raised = float(campaign["raised_amount"] or 0)

        if target > 0:
            campaign["progress"] = min(
                round((raised / target) * 100),
                100
            )
        else:
            campaign["progress"] = 0

    return render_template(
        "Campaign.html",
        campaigns=campaigns
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not is_valid_csrf_token():
            return "Invalid CSRF token. Please refresh the page and try again.", 400
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        cursor = db.cursor()

        try:
            query = "SELECT * FROM users WHERE email=%s"
            cursor.execute(query, (email,))
            user = cursor.fetchone()
        finally:
            cursor.close()

        if user and check_password_hash(user[4], password):
            remember_me = request.form.get("remember_me") == "yes"
            
            session.clear()
            session.permanent = remember_me

            session["user_id"] = user[0]
            session["email"] = user[2]

            return redirect(url_for("dashboard"))

        return render_template(
            "login.html",
            error="Incorrect email or password"
        )

    return render_template("login.html")

    

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        phone = request.form["phone"]
        if not re.fullmatch(r"[6-9]\d{9}", phone):
            error = "Please enter a valid 10-digit Indian mobile number."
            return render_template("signup.html", error=error)
        password = request.form["password"]
        confirm_password = request.form["confirm-password"]

        if password != confirm_password:
            return render_template(
        "signup.html",
        error="Passwords do not match!",
        error_title="Password Mismatch"
    )

        hashed_password = generate_password_hash(password)

        cursor = db.cursor()

        query = """
        INSERT INTO users(full_name, email, phone, password)
        VALUES (%s, %s, %s, %s)
        """

        try:
            cursor.execute(
                query,
                (name, email, phone, hashed_password)
            )
            db.commit()

        except IntegrityError as e:
            db.rollback()

            if e.errno == 1062:
                cursor.close()
                return render_template(
                    "signup.html",
                    error_title="Email Already Registered"
                )

            cursor.close()
            return render_template(
                "signup.html",
                error="Something went wrong. Please try again."
            )

        cursor.close()

        return redirect(url_for("login"))

    return render_template("signup.html")




@app.route("/how-it-works")
def how_it_works():
    return render_template("HowItWorks.html")


@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    is_admin = is_admin_user()

    cursor = db.cursor(dictionary=True)

    # User details
    cursor.execute("""
        SELECT user_id, full_name, email
        FROM users
        WHERE user_id = %s
    """, (user_id,))

    user = cursor.fetchone()

    # Donation history
    cursor.execute("""
        SELECT
            d.donation_id,
            c.campaign_name,
            d.amount,
            d.donation_date,
            d.status
        FROM donations d
        JOIN campaigns c ON d.campaign_id = c.campaign_id
        WHERE d.user_id = %s
        ORDER BY d.donation_date DESC
    """, (user_id,))

    donations = cursor.fetchall()

    # Total donated
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total_donated
        FROM donations
        WHERE user_id = %s
    """, (user_id,))

    total_donated = cursor.fetchone()["total_donated"]

    # Number of donations
    cursor.execute("""
        SELECT COUNT(*) AS donation_count
        FROM donations
        WHERE user_id = %s
    """, (user_id,))

    donation_count = cursor.fetchone()["donation_count"]

    # Number of campaigns supported
    cursor.execute("""
        SELECT COUNT(DISTINCT campaign_id) AS campaign_count
        FROM donations
        WHERE user_id = %s
    """, (user_id,))

    campaign_count = cursor.fetchone()["campaign_count"]

# Campaigns supported by the user
    cursor.execute("""
    SELECT
        c.campaign_id,
        c.campaign_name,
        c.description,
        SUM(d.amount) AS total_donated
    FROM donations d
    JOIN campaigns c
        ON d.campaign_id = c.campaign_id
    WHERE d.user_id = %s
    GROUP BY
        c.campaign_id,
        c.campaign_name,
        c.description
    ORDER BY c.campaign_id
""", (user_id,))

    supported_campaigns = cursor.fetchall()

    impact = int(total_donated // 500)

    cursor.close()

    return render_template(
    "dashboard.html",
    user=user,
    donations=donations,
    total_donated=total_donated,
    donation_count=donation_count,
    campaign_count=campaign_count,
    supported_campaigns=supported_campaigns,
    impact=impact,
    is_admin=is_admin
)

@app.route("/admin/users")
def admin_users():

    if "user_id" not in session:
        return redirect(url_for("login"))

    # Only admin can access
    if not is_admin_user():
        return "Access Denied", 403

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            user_id,
            full_name,
            email,
            phone,
            created_at,
            role
        FROM users
        ORDER BY user_id DESC
    """)

    users = cursor.fetchall()

    cursor.close()

    return render_template(
        "dashboard.html",
        admin_users=True,
        users=users,
        is_admin=True
    )


@app.route("/admin/donations")
def admin_donations():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    cursor = db.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            d.donation_id,
            u.full_name,
            c.campaign_name,
            d.amount,
            d.payment_method,
            d.donation_date,
            d.status
        FROM donations d
        JOIN users u
            ON d.user_id = u.user_id
        JOIN campaigns c
            ON d.campaign_id = c.campaign_id
        ORDER BY d.donation_date DESC
    """)

    donations = cursor.fetchall()

    for donation in donations:

        if float(donation["amount"]) >= 10000:
            donation["suspicious"] = True
            donation["reason"] = "Unusually large donation"
        else:
            donation["suspicious"] = False
            donation["reason"] = "Normal donation"


    transfer_cursor = db.cursor(dictionary=True)

    transfer_cursor.execute("""
        SELECT
            ct.transfer_id,
            source.campaign_name AS source_campaign,
            target.campaign_name AS target_campaign,
            ct.amount,
            ct.transferred_at
        FROM campaign_transfers ct
        JOIN campaigns source
            ON ct.source_campaign_id = source.campaign_id
        JOIN campaigns target
            ON ct.target_campaign_id = target.campaign_id
        ORDER BY ct.transferred_at DESC
    """)

    surplus_transfers = transfer_cursor.fetchall()

    transfer_cursor.close()


    cursor.close()

    return render_template(
        "dashboard.html",
        admin_donations=True,
        donations=donations,
        surplus_transfers=surplus_transfers,
        is_admin=True
    )


@app.route("/admin/reports")
def admin_reports():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    cursor = db.cursor(dictionary=True)

    # Total donations
    cursor.execute("""
        SELECT COUNT(*) AS total_donations
        FROM donations
        WHERE status = 'Completed'
    """)
    total_donations = cursor.fetchone()["total_donations"]

    # Total amount
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total_amount
        FROM donations
        WHERE status = 'Completed'
    """)
    total_amount = cursor.fetchone()["total_amount"]


        # Total expenses
    cursor.execute("""
        SELECT COALESCE(SUM(amount), 0) AS total_expenses
        FROM campaign_expenses
    """)
    total_expenses = cursor.fetchone()["total_expenses"]

    # Actual remaining balance
    remaining_balance = float(total_amount) - float(total_expenses)

    # Total donors
    cursor.execute("""
        SELECT COUNT(DISTINCT user_id) AS total_donors
        FROM donations
        WHERE status = 'Completed'
    """)
    total_donors = cursor.fetchone()["total_donors"]

    # Total campaigns
    cursor.execute("""
        SELECT COUNT(*) AS total_campaigns
        FROM campaigns
    """)
    total_campaigns = cursor.fetchone()["total_campaigns"]

        # Campaign-wise fund tracking
    cursor.execute("""
        SELECT
            campaign_id,
            campaign_name,
            target_amount,
            raised_amount,
            category
        FROM campaigns
        ORDER BY campaign_id
    """)

    campaign_reports = cursor.fetchall()

    for campaign in campaign_reports:

        target = float(campaign["target_amount"])
        raised = float(campaign["raised_amount"])

        remaining = target - raised

        if remaining < 0:
            remaining = 0

        if target > 0:
            progress = min(round((raised / target) * 100), 100)
        else:
            progress = 0

        campaign["remaining"] = remaining
        campaign["progress"] = progress
        campaign["target_achieved"] = raised >= target


    cursor.close()

    return render_template(
    "dashboard.html",
    admin_reports=True,
    total_donations=total_donations,
    total_amount=total_amount,
    total_donors=total_donors,
    total_campaigns=total_campaigns,
    total_expenses=total_expenses,
    remaining_balance=remaining_balance,
    campaign_reports=campaign_reports,
    is_admin=True
)



@app.route('/admin/expenses', methods=['GET', 'POST'])
def admin_expenses():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT role FROM users WHERE user_id = %s",
        (session['user_id'],)
    )
    current_user = cursor.fetchone()

    if not current_user or current_user['role'] != 'admin':
        cursor.close()
        return redirect(url_for('index'))

    if request.method == 'POST':
        if not is_valid_csrf_token():
            cursor.close()
            return "Invalid CSRF token. Please refresh the page and try again.", 400

        campaign_id = request.form.get('campaign_id', type=int)
        expense_title = request.form.get('expense_title', '').strip()
        description = request.form.get('description', '').strip()
        amount_text = request.form.get('amount', '').strip()
        expense_date = request.form.get('expense_date', '').strip()

        try:
            amount = float(amount_text)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError
        except (ValueError, TypeError):
            cursor.close()
            return "Please enter a valid positive expense amount.", 400

        if not campaign_id or not expense_title or not expense_date:
            cursor.close()
            return "Please fill all required fields.", 400

        try:
            cursor.execute(
                "SELECT campaign_id FROM campaigns WHERE campaign_id = %s",
                (campaign_id,)
            )
            campaign = cursor.fetchone()

            if not campaign:
                cursor.close()
                return "Campaign not found.", 404

            cursor.execute("""
                INSERT INTO campaign_expenses
                (campaign_id, expense_title, description, amount, expense_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                campaign_id,
                expense_title,
                description or None,
                amount,
                expense_date
            ))
            db.commit()

        except Exception:
            db.rollback()
            cursor.close()
            raise

        cursor.close()
        return redirect(url_for('admin_expenses'))

    cursor.execute("""
        SELECT campaign_id, campaign_name
        FROM campaigns
        ORDER BY campaign_name
    """)
    campaigns = cursor.fetchall()

    cursor.execute("""
        SELECT e.*, c.campaign_name
        FROM campaign_expenses e
        JOIN campaigns c ON e.campaign_id = c.campaign_id
        ORDER BY e.recorded_at DESC
    """)
    expenses = cursor.fetchall()

    cursor.close()
    return render_template(
        'admin_expenses.html',
        campaigns=campaigns,
        expenses=expenses
    )





@app.route("/admin/powerbi-analytics")
def powerbi_analytics():
    """Native Flask/MySQL analytics dashboard for administrators."""
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT
                COUNT(*) AS total_donations,
                COALESCE(SUM(amount), 0) AS total_amount,
                COUNT(DISTINCT user_id) AS total_donors
            FROM donations
            WHERE status = 'Completed'
        """)
        summary = cursor.fetchone() or {}

        cursor.execute("""
            SELECT DATE_FORMAT(donation_date, '%Y-%m') AS month_key,
                   DATE_FORMAT(donation_date, '%b %Y') AS month_label,
                   COALESCE(SUM(amount), 0) AS amount,
                   COUNT(*) AS donation_count
            FROM donations
            WHERE status = 'Completed'
              AND donation_date >= DATE_SUB(CURDATE(), INTERVAL 5 MONTH)
            GROUP BY DATE_FORMAT(donation_date, '%Y-%m'),
                     DATE_FORMAT(donation_date, '%b %Y')
            ORDER BY month_key
        """)
        monthly_rows = cursor.fetchall()

        cursor.execute("""
            SELECT COALESCE(c.category, 'Uncategorized') AS category,
                   COALESCE(SUM(d.amount), 0) AS amount,
                   COUNT(d.donation_id) AS donation_count
            FROM donations d
            JOIN campaigns c ON c.campaign_id = d.campaign_id
            WHERE d.status = 'Completed'
            GROUP BY c.category
            ORDER BY amount DESC
        """)
        category_rows = cursor.fetchall()

        cursor.execute("""
            SELECT c.campaign_name,
                   COALESCE(SUM(d.amount), 0) AS amount,
                   COUNT(d.donation_id) AS donation_count,
                   c.target_amount,
                   c.raised_amount
            FROM campaigns c
            LEFT JOIN donations d
              ON d.campaign_id = c.campaign_id
             AND d.status = 'Completed'
            GROUP BY c.campaign_id, c.campaign_name,
                     c.target_amount, c.raised_amount
            ORDER BY amount DESC, c.campaign_name
            LIMIT 8
        """)
        campaign_rows = cursor.fetchall()

        cursor.execute("""
            SELECT COALESCE(payment_method, 'Unknown') AS payment_method,
                   COUNT(*) AS donation_count,
                   COALESCE(SUM(amount), 0) AS amount
            FROM donations
            WHERE status = 'Completed'
            GROUP BY payment_method
            ORDER BY amount DESC
        """)
        payment_rows = cursor.fetchall()

        cursor.execute("""
            SELECT d.donation_id, d.amount, d.payment_method,
                   d.donation_date, d.status,
                   u.full_name, c.campaign_name
            FROM donations d
            LEFT JOIN users u ON u.user_id = d.user_id
            LEFT JOIN campaigns c ON c.campaign_id = d.campaign_id
            ORDER BY d.donation_date DESC, d.donation_id DESC
            LIMIT 8
        """)
        recent_donations = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) AS total_campaigns FROM campaigns")
        total_campaigns = (cursor.fetchone() or {}).get("total_campaigns", 0)

    finally:
        cursor.close()

    # Convert MySQL Decimal values to JSON-safe numbers for Chart.js.
    for row in monthly_rows:
        row["amount"] = float(row.get("amount") or 0)
        row["donation_count"] = int(row.get("donation_count") or 0)
    for row in category_rows:
        row["amount"] = float(row.get("amount") or 0)
        row["donation_count"] = int(row.get("donation_count") or 0)
    for row in campaign_rows:
        row["amount"] = float(row.get("amount") or 0)
        row["target_amount"] = float(row.get("target_amount") or 0)
        row["raised_amount"] = float(row.get("raised_amount") or 0)
        row["donation_count"] = int(row.get("donation_count") or 0)
    for row in payment_rows:
        row["amount"] = float(row.get("amount") or 0)
        row["donation_count"] = int(row.get("donation_count") or 0)

    return render_template(
        "powerbi_analytics.html",
        summary=summary,
        total_campaigns=total_campaigns,
        monthly_rows=monthly_rows,
        category_rows=category_rows,
        campaign_rows=campaign_rows,
        payment_rows=payment_rows,
        recent_donations=recent_donations,
    )



@app.route("/admin/campaigns", methods=["GET", "POST"])
def admin_campaigns():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    # ADD NEW CAMPAIGN
    if request.method == "POST":

        if not is_valid_csrf_token():
            return "Invalid CSRF token. Please refresh the page and try again.", 400

        campaign_name = request.form.get("campaign_name", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        target_text = request.form.get("target_amount", "").strip()

        # Validate text fields
        if not campaign_name or len(campaign_name) > 100:
            return "Campaign name is required and must be at most 100 characters.", 400

        if not category or len(category) > 50:
            return "Category is required and must be at most 50 characters.", 400

        if not description:
            return "Campaign description is required.", 400

        # Validate target amount
        try:
            target_amount = float(target_text)
        except (ValueError, TypeError):
            return "Please enter a valid target amount.", 400

        if not math.isfinite(target_amount) or target_amount <= 0:
            return "Target amount must be a finite number greater than zero.", 400

        # Validate uploaded image
        image = request.files.get("campaign_image")
        image_filename = None
        image_data = None
        image_path = None

        if image and image.filename:
            original_filename = secure_filename(image.filename)
            allowed_extensions = {"png", "jpg", "jpeg", "webp"}

            if (
                "." not in original_filename
                or original_filename.rsplit(".", 1)[1].lower()
                not in allowed_extensions
            ):
                return "Invalid image type. Use PNG, JPG, JPEG or WEBP.", 400

            # Limit image data to 5 MB
            image_data = image.read(5 * 1024 * 1024 + 1)

            if len(image_data) > 5 * 1024 * 1024:
                return "Image size must not exceed 5 MB.", 400

            # Check actual file signature, not only its extension
            extension = original_filename.rsplit(".", 1)[1].lower()

            is_png = image_data.startswith(b"\x89PNG\r\n\x1a\n")
            is_jpeg = image_data.startswith(b"\xff\xd8\xff")
            is_webp = (
                len(image_data) >= 12
                and image_data[:4] == b"RIFF"
                and image_data[8:12] == b"WEBP"
            )

            valid_image = (
                (extension == "png" and is_png)
                or (extension in {"jpg", "jpeg"} and is_jpeg)
                or (extension == "webp" and is_webp)
            )

            if not valid_image:
                return "The uploaded file is not a valid image matching its extension.", 400

            image_filename = f"{uuid4().hex}.{extension}"

            upload_folder = os.path.join(
                app.static_folder, "uploads", "campaigns"
            )
            os.makedirs(upload_folder, exist_ok=True)

            image_path = os.path.join(upload_folder, image_filename)

        cursor = db.cursor()

        try:
            # Save the image only after validation
            if image_data is not None:
                with open(image_path, "wb") as image_file:
                    image_file.write(image_data)

            cursor.execute("""
                INSERT INTO campaigns
                (campaign_name, description, target_amount,
                 raised_amount, category, image_filename)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                campaign_name,
                description,
                target_amount,
                0,
                category,
                image_filename
            ))

            db.commit()

        except Exception:
            db.rollback()

            # Remove the saved image if database insertion fails
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                except OSError:
                    app.logger.exception("Could not remove uploaded campaign image")

            app.logger.exception("Campaign creation failed")
            return "Unable to create campaign. Please check the input and try again.", 500

        finally:
            cursor.close()

        return redirect(url_for("admin_campaigns"))

    # SHOW ALL CAMPAIGNS
    cursor = db.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                campaign_id,
                campaign_name,
                description,
                target_amount,
                raised_amount,
                category,
                image_filename
            FROM campaigns
            ORDER BY campaign_id DESC
        """)

        campaigns = cursor.fetchall()

    finally:
        cursor.close()

    return render_template(
        "dashboard.html",
        admin_campaigns=True,
        campaigns=campaigns,
        is_admin=True
    )


@app.route("/admin/campaigns/delete/<int:campaign_id>", methods=["POST"])
def delete_campaign(campaign_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    if not is_valid_csrf_token():
        return "Invalid CSRF token. Please refresh the page and try again.", 400

    cursor = db.cursor()

    try:
        # Check whether donations exist
        cursor.execute("""
            SELECT COUNT(*) FROM donations
            WHERE campaign_id = %s
        """, (campaign_id,))

        donation_count = cursor.fetchone()[0]

        if donation_count > 0:
            return """
            <script>
                alert("This campaign cannot be deleted because donations already exist for it.");
                window.location.href = "/admin/campaigns";
            </script>
            """

        # Check whether campaign transfers exist
        cursor.execute("""
            SELECT COUNT(*) FROM campaign_transfers
            WHERE source_campaign_id = %s
               OR target_campaign_id = %s
        """, (campaign_id, campaign_id))

        transfer_count = cursor.fetchone()[0]

        if transfer_count > 0:
            return """
            <script>
                alert("This campaign cannot be deleted because it is linked to campaign transfers.");
                window.location.href = "/admin/campaigns";
            </script>
            """

        # Delete campaign
        cursor.execute("""
            DELETE FROM campaigns
            WHERE campaign_id = %s
        """, (campaign_id,))

        db.commit()

        if cursor.rowcount == 0:
            return "Campaign not found.", 404

    except IntegrityError:
        db.rollback()
        return "Campaign cannot be deleted because related records exist.", 400

    except Exception:
        db.rollback()
        app.logger.exception("Campaign deletion failed")
        return "Unable to delete campaign. Please try again.", 500

    finally:
        cursor.close()

    return redirect(url_for("admin_campaigns"))



@app.route("/admin/campaigns/allocate-surplus/<int:source_campaign_id>", methods=["POST"])
def allocate_surplus(source_campaign_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if not is_admin_user():
        return "Access Denied", 403

    cursor = db.cursor(dictionary=True)

    target_campaign_id = request.form["target_campaign_id"]
    amount = float(request.form["amount"])

    # Source campaign
    cursor.execute("""
        SELECT campaign_id, target_amount, raised_amount
        FROM campaigns
        WHERE campaign_id = %s
    """, (source_campaign_id,))

    source = cursor.fetchone()

    if not source:
        cursor.close()
        return "Source campaign not found", 404

    surplus = float(source["raised_amount"]) - float(source["target_amount"])

    if surplus <= 0:
        cursor.close()
        return "No surplus available", 400

    if amount <= 0 or amount > surplus:
        cursor.close()
        return "Invalid surplus amount", 400

    # Target campaign
    cursor.execute("""
        SELECT campaign_id, target_amount, raised_amount
        FROM campaigns
        WHERE campaign_id = %s
    """, (target_campaign_id,))

    target = cursor.fetchone()

    if not target:
        cursor.close()
        return "Target campaign not found", 404

    if target_campaign_id == source_campaign_id:
        cursor.close()
        return "Source and target campaigns cannot be same", 400

    # Save transfer record
    cursor.execute("""
        INSERT INTO campaign_transfers
        (source_campaign_id, target_campaign_id, amount)
        VALUES (%s, %s, %s)
    """, (
        source_campaign_id,
        target_campaign_id,
        amount
    ))

    # Deduct from source
    cursor.execute("""
        UPDATE campaigns
        SET raised_amount = raised_amount - %s
        WHERE campaign_id = %s
    """, (amount, source_campaign_id))

    # Add to target
    cursor.execute("""
        UPDATE campaigns
        SET raised_amount = raised_amount + %s
        WHERE campaign_id = %s
    """, (amount, target_campaign_id))

    db.commit()
    cursor.close()

    return redirect(url_for("admin_campaigns"))




@app.route("/demo-payment/<payment_token>")
def demo_payment(payment_token):

    payment = demo_payments.get(payment_token)

    if not payment:
        return "Payment session expired or not found.", 404

    return render_template(
        "Donation.html",
        phone_payment=True,
        payment=payment,
        payment_token=payment_token
    )

@app.route("/demo-pay/<payment_token>", methods=["POST"])
def demo_pay(payment_token):

    payment = demo_payments.get(payment_token)

    if not payment:
        return "Payment session expired.", 404

    if not is_valid_csrf_token():
        return "Invalid payment request.", 400

    cursor = db.cursor()

    try:
        cursor.execute("""
            SELECT raised_amount, target_amount
            FROM campaigns
            WHERE campaign_id = %s
            FOR UPDATE
        """, (payment["campaign_id"],))

        campaign = cursor.fetchone()

        if not campaign:
            db.rollback()
            return "Campaign not found.", 404

        if payment.get("status") == "paid":
            db.rollback()
            return redirect(
                url_for("demo_payment", payment_token=payment_token)
            )

        if float(campaign[0]) >= float(campaign[1]):
            db.rollback()
            return "Campaign target achieved.", 400

        cursor.execute("""
            INSERT INTO donations
            (user_id, campaign_id, amount, payment_method)
            VALUES (%s, %s, %s, %s)
        """, (
            payment["user_id"],
            payment["campaign_id"],
            payment["amount"],
            "upi"
        ))

        donation_id = cursor.lastrowid

        cursor.execute("""
            UPDATE campaigns
            SET raised_amount = raised_amount + %s
            WHERE campaign_id = %s
        """, (
            payment["amount"],
            payment["campaign_id"]
        ))

        db.commit()

        payment["status"] = "paid"
        payment["donation_id"] = donation_id

    except Exception:
        db.rollback()
        raise

    finally:
        cursor.close()

    return redirect(
        url_for("demo_payment", payment_token=payment_token)
    )

@app.route("/payment-status/<payment_token>")
def payment_status(payment_token):

    if "user_id" not in session:
        response = jsonify({"status": "unauthorized"})
        response.status_code = 401
        response.headers["Cache-Control"] = "no-store"
        return response

    payment = demo_payments.get(payment_token)

    if not payment:
        response = jsonify({"status": "not_found"})
        response.status_code = 404
        response.headers["Cache-Control"] = "no-store"
        return response

    if payment["user_id"] != session["user_id"]:
        response = jsonify({"status": "forbidden"})
        response.status_code = 403
        response.headers["Cache-Control"] = "no-store"
        return response

    response = jsonify({
        "status": payment["status"],
        "donation_id": payment.get("donation_id")
    })
    response.headers["Cache-Control"] = "no-store"
    return response



if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
