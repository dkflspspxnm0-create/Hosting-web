import os
import sqlite3
import uuid
import subprocess
import signal
from pathlib import Path

from flask import Flask, request, redirect, session, render_template, abort


# =========================
# GODX HOSTING CONFIG
# =========================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DATABASE = DATA_DIR / "godx.db"

DATA_DIR.mkdir(exist_ok=True)
PROJECTS_DIR.mkdir(exist_ok=True)


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY"
)

# Maximum upload size
MAX_UPLOAD_MB = int(
    os.environ.get("MAX_UPLOAD_MB", "500")
)

app.config["MAX_CONTENT_LENGTH"] = (
    MAX_UPLOAD_MB * 1024 * 1024
)


# =========================
# ADMIN VARIABLES
# =========================

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "godx_raftaar21"
)

ADMIN_EMAIL = os.environ.get(
    "ADMIN_EMAIL",
    ""
)

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "CHANGE_THIS_PASSWORD"
)


# =========================
# RUNNING PROCESSES
# =========================

RUNNING_PROCESSES = {}


# =========================
# DATABASE
# =========================

def get_db():

    database = sqlite3.connect(DATABASE)

    database.row_factory = sqlite3.Row

    return database


def init_database():

    database = get_db()

    database.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    database.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            folder TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'stopped',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    database.commit()

    database.close()


init_database()


# =========================
# PROJECT HELPER
# =========================

def get_user_project(project_id):

    if not session.get("user_id"):
        return None

    database = get_db()

    project = database.execute(
        """
        SELECT *
        FROM projects
        WHERE id = ?
        AND user_id = ?
        """,
        (
            project_id,
            session["user_id"]
        )
    ).fetchone()

    database.close()

    return project


# =========================
# PROJECT COMMAND
# =========================

def get_project_command(project):

    folder = Path(project["folder"])

    if not folder.exists():
        return None


    # -------------------------
    # PYTHON
    # -------------------------

    if project["kind"] == "Python":

        python_file = next(
            (
                file
                for file in folder.iterdir()
                if file.suffix.lower() == ".py"
            ),
            None
        )

        if python_file:

            return [
                os.environ.get(
                    "PYTHON_BIN",
                    "python3"
                ),
                str(python_file)
            ]


    # -------------------------
    # NODE.JS
    # -------------------------

    if project["kind"] == "Node.js":

        node_file = next(
            (
                file
                for file in folder.iterdir()
                if file.suffix.lower() == ".js"
            ),
            None
        )

        if node_file:

            return [
                "node",
                str(node_file)
            ]


    # -------------------------
    # JAVA
    # -------------------------

    if project["kind"] == "Java":

        jar_file = next(
            (
                file
                for file in folder.iterdir()
                if file.suffix.lower() == ".jar"
            ),
            None
        )

        if jar_file:

            return [
                "java",
                "-jar",
                str(jar_file)
            ]


    # -------------------------
    # PHP
    # -------------------------

    if project["kind"] == "PHP":

        php_file = next(
            (
                file
                for file in folder.iterdir()
                if file.suffix.lower() == ".php"
            ),
            None
        )

        if php_file:

            return [
                "php",
                str(php_file)
            ]


    return None


# =========================
# HOME
# =========================

@app.route("/")
def home():

    if session.get("admin"):

        return redirect("/admin")


    if not session.get("user_id"):

        return redirect("/login")


    database = get_db()

    projects = database.execute(
        """
        SELECT *
        FROM projects
        WHERE user_id = ?
        ORDER BY created_at DESC
        """,
        (
            session["user_id"],
        )
    ).fetchall()

    database.close()

    return render_template(
        "dashboard.html",
        projects=projects
    )


# =========================
# REGISTER
# =========================

@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )


        if not email or not password:

            return render_template(
                "auth.html",
                mode="register",
                error="Email and password are required."
            )


        if len(password) < 6:

            return render_template(
                "auth.html",
                mode="register",
                error="Password must be at least 6 characters."
            )


        try:

            database = get_db()

            database.execute(
                """
                INSERT INTO users
                (email, password)
                VALUES (?, ?)
                """,
                (
                    email,
                    password
                )
            )

            database.commit()


            user = database.execute(
                """
                SELECT id
                FROM users
                WHERE email = ?
                """,
                (
                    email,
                )
            ).fetchone()


            database.close()


            session.clear()

            session["user_id"] = user["id"]

            return redirect("/")


        except sqlite3.IntegrityError:

            return render_template(
                "auth.html",
                mode="register",
                error="This email is already registered."
            )


    return render_template(
        "auth.html",
        mode="register",
        error=""
    )


# =========================
# LOGIN
# =========================

@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )


        # ADMIN LOGIN

        if (
            email == ADMIN_EMAIL
            and
            password == ADMIN_PASSWORD
        ):

            session.clear()

            session["admin"] = True

            return redirect("/admin")


        # USER LOGIN

        database = get_db()

        user = database.execute(
            """
            SELECT *
            FROM users
            WHERE email = ?
            AND password = ?
            """,
            (
                email,
                password
            )
        ).fetchone()

        database.close()


        if user:

            session.clear()

            session["user_id"] = user["id"]

            return redirect("/")


        return render_template(
            "auth.html",
            mode="login",
            error="Invalid email or password."
        )


    return render_template(
        "auth.html",
        mode="login",
        error=""
    )


# =========================
# LOGOUT
# =========================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# =========================
# CREATE PROJECT
# =========================

@app.route(
    "/create",
    methods=["GET", "POST"]
)
def create_project():

    if not session.get("user_id"):

        return redirect("/login")


    database = get_db()

    count = database.execute(
        """
        SELECT COUNT(*) AS total
        FROM projects
        WHERE user_id = ?
        """,
        (
            session["user_id"],
        )
    ).fetchone()["total"]

    database.close()


    # FREE PLAN LIMIT

    if count >= 2:

        return render_template(
            "message.html",
            message="Free plan limit: 2 projects."
        )


    if request.method == "POST":

        project_name = request.form.get(
            "name",
            ""
        ).strip()

        project_type = request.form.get(
            "kind",
            ""
        )

        uploaded_file = request.files.get(
            "file"
        )


        if (
            not project_name
            or
            not uploaded_file
            or
            not uploaded_file.filename
        ):

            return render_template(
                "message.html",
                message="Project name and file are required."
            )


        project_id = uuid.uuid4().hex[:12]

        project_folder = (
            PROJECTS_DIR /
            project_id
        )

        project_folder.mkdir(
            parents=True,
            exist_ok=True
        )


        # Keep only filename, preventing path traversal

        safe_filename = Path(
            uploaded_file.filename
        ).name


        uploaded_file.save(
            project_folder /
            safe_filename
        )


        database = get_db()

        database.execute(
            """
            INSERT INTO projects
            (
                id,
                user_id,
                name,
                kind,
                folder
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                project_id,
                session["user_id"],
                project_name,
                project_type,
                str(project_folder)
            )
        )

        database.commit()

        database.close()


        return redirect(
            "/project/" + project_id
        )


    return render_template(
        "create.html"
    )


# =========================
# PROJECT PAGE
# =========================

@app.route(
    "/project/<project_id>"
)
def project_page(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    log_file = (
        Path(project["folder"])
        /
        "runtime.log"
    )


    if log_file.exists():

        logs = log_file.read_text(
            errors="ignore"
        )[-15000:]

    else:

        logs = "No logs yet."


    return render_template(
        "project.html",
        project=project,
        logs=logs
    )


# =========================
# START PROJECT
# =========================

@app.post(
    "/project/<project_id>/start"
)
def start_project(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    command = get_project_command(
        project
    )


    if not command:

        return render_template(
            "message.html",
            message="No runnable file found for this project type."
        )


    old_process = RUNNING_PROCESSES.get(
        project_id
    )


    if (
        old_process
        and
        old_process.poll() is None
    ):

        return redirect(
            "/project/" + project_id
        )


    log_file = (
        Path(project["folder"])
        /
        "runtime.log"
    )


    log = open(
        log_file,
        "a",
        encoding="utf-8"
    )


    try:

        process = subprocess.Popen(
            command,
            cwd=project["folder"],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )


        RUNNING_PROCESSES[
            project_id
        ] = process


        database = get_db()

        database.execute(
            """
            UPDATE projects
            SET status = 'running'
            WHERE id = ?
            """,
            (
                project_id,
            )
        )

        database.commit()

        database.close()


    except Exception as error:

        log.write(
            "\nSTART ERROR: "
            +
            repr(error)
            +
            "\n"
        )

        log.close()


    return redirect(
        "/project/" + project_id
    )


# =========================
# STOP PROJECT
# =========================

@app.post(
    "/project/<project_id>/stop"
)
def stop_project(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    process = RUNNING_PROCESSES.pop(
        project_id,
        None
    )


    if (
        process
        and
        process.poll() is None
    ):

        try:

            os.killpg(
                process.pid,
                signal.SIGTERM
            )

        except Exception:

            try:

                process.terminate()

            except Exception:

                pass


    database = get_db()

    database.execute(
        """
        UPDATE projects
        SET status = 'stopped'
        WHERE id = ?
        """,
        (
            project_id,
        )
    )

    database.commit()

    database.close()


    return redirect(
        "/project/" + project_id
    )


# =========================
# DELETE PROJECT
# =========================

@app.post(
    "/project/<project_id>/delete"
)
def delete_project(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    # Stop process first

    process = RUNNING_PROCESSES.pop(
        project_id,
        None
    )


    if (
        process
        and
        process.poll() is None
    ):

        try:

            os.killpg(
                process.pid,
                signal.SIGTERM
            )

        except Exception:

            pass


    # Delete project files

    project_folder = Path(
        project["folder"]
    )


    if project_folder.exists():

        import shutil

        shutil.rmtree(
            project_folder,
            ignore_errors=True
        )


    # Delete database record

    database = get_db()

    database.execute(
        """
        DELETE FROM projects
        WHERE id = ?
        """,
        (
            project_id,
        )
    )

    database.commit()

    database.close()


    return redirect("/")


# =========================
# ADMIN PANEL
# =========================

@app.route("/admin")
def admin_panel():

    if not session.get("admin"):

        return redirect("/login")


    database = get_db()


    total_users = database.execute(
        """
        SELECT COUNT(*) AS total
        FROM users
        """
    ).fetchone()["total"]


    total_projects = database.execute(
        """
        SELECT COUNT(*) AS total
        FROM projects
        """
    ).fetchone()["total"]


    projects = database.execute(
        """
        SELECT
            projects.*,
            users.email
        FROM projects
        JOIN users
        ON users.id = projects.user_id
        ORDER BY projects.created_at DESC
        """
    ).fetchall()


    database.close()


    return render_template(
        "admin.html",
        users=total_users,
        projects=total_projects,
        rows=projects
    )


# =========================
# RAILWAY START
# =========================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8080"
        )
    )


    app.run(
        host="0.0.0.0",
        port=port
)
