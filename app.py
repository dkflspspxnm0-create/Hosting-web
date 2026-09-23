import os
import sys
import json
import uuid
import signal
import shutil
import subprocess
import threading
from pathlib import Path

from flask import (
    Flask,
    request,
    redirect,
    session,
    render_template,
    abort,
    send_from_directory,
    url_for
)

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename


# ============================================================
# GODX HOSTING CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DATABASE = DATA_DIR / "godx.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY"
)


# ============================================================
# UPLOAD LIMIT
# ============================================================

MAX_UPLOAD_MB = int(
    os.environ.get(
        "MAX_UPLOAD_MB",
        "500"
    )
)

app.config["MAX_CONTENT_LENGTH"] = (
    MAX_UPLOAD_MB * 1024 * 1024
)


# ============================================================
# ADMIN VARIABLES
# ============================================================

ADMIN_USERNAME = os.environ.get(
    "ADMIN_USERNAME",
    "godx_raftaar21"
)

ADMIN_EMAIL = os.environ.get(
    "ADMIN_EMAIL",
    ""
).strip().lower()

ADMIN_PASSWORD = os.environ.get(
    "ADMIN_PASSWORD",
    "CHANGE_THIS_PASSWORD"
)


# ============================================================
# RUNNING PROCESSES
# ============================================================

RUNNING_PROCESSES = {}

PROCESS_LOG_FILES = {}


# ============================================================
# DATABASE
# ============================================================

def get_db():

    database = sqlite3.connect(
        DATABASE,
        timeout=30
    )

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

    database.execute("""
        CREATE TABLE IF NOT EXISTS project_env (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            env_key TEXT NOT NULL,
            env_value TEXT NOT NULL,
            UNIQUE(project_id, env_key)
        )
    """)

    database.commit()

    database.close()


# sqlite3 is imported after the helper section in the
# original project. Keep it explicit here.
import sqlite3

init_database()


# ============================================================
# GENERAL HELPERS
# ============================================================

ALLOWED_PROJECT_TYPES = {
    "Python",
    "Node.js",
    "Java",
    "PHP",
    "Static"
}


def project_path(project):

    return Path(
        project["folder"]
    ).resolve()


def is_safe_project_path(path):

    try:

        path.relative_to(
            PROJECTS_DIR.resolve()
        )

        return True

    except ValueError:

        return False


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


def get_project_env(project_id):

    database = get_db()

    rows = database.execute(
        """
        SELECT env_key, env_value
        FROM project_env
        WHERE project_id = ?
        ORDER BY id ASC
        """,
        (
            project_id,
        )
    ).fetchall()

    database.close()

    return {
        row["env_key"]: row["env_value"]
        for row in rows
    }


def save_project_env(project_id, keys, values):

    database = get_db()

    database.execute(
        """
        DELETE FROM project_env
        WHERE project_id = ?
        """,
        (
            project_id,
        )
    )

    for key, value in zip(keys, values):

        key = (key or "").strip()
        value = value or ""

        if not key:
            continue

        # Environment variable names should be simple.
        if not key.replace("_", "").isalnum():
            continue

        database.execute(
            """
            INSERT OR REPLACE INTO project_env
            (
                project_id,
                env_key,
                env_value
            )
            VALUES (?, ?, ?)
            """,
            (
                project_id,
                key,
                value
            )
        )

    database.commit()

    database.close()


def safe_project_files(project_folder):

    files = []

    if not project_folder.exists():

        return files

    for item in project_folder.rglob("*"):

        if not item.is_file():
            continue

        if item.name == "runtime.log":
            continue

        try:

            relative = item.relative_to(
                project_folder
            )

            files.append(relative)

        except ValueError:

            pass

    return files


# ============================================================
# FILE DETECTION
# ============================================================

def find_file(folder, suffixes, preferred_names=None):

    preferred_names = preferred_names or []

    files = safe_project_files(folder)

    # First check preferred names.
    for preferred in preferred_names:

        for relative in files:

            if relative.name.lower() == preferred.lower():

                return folder / relative


    # Then find by extension.
    for relative in files:

        if relative.suffix.lower() in suffixes:

            return folder / relative


    return None


# ============================================================
# PYTHON DEPENDENCIES
# ============================================================

def install_python_requirements(folder):

    requirements_file = folder / "requirements.txt"

    if not requirements_file.exists():

        return True, "No requirements.txt found. Skipping dependency installation."


    deps_dir = folder / ".godx_deps"

    deps_dir.mkdir(
        parents=True,
        exist_ok=True
    )


    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--target",
        str(deps_dir),
        "-r",
        str(requirements_file)
    ]


    try:

        result = subprocess.run(
            command,
            cwd=str(folder),
            capture_output=True,
            text=True,
            timeout=600
        )


        if result.returncode != 0:

            return (
                False,
                result.stdout +
                "\n" +
                result.stderr
            )


        return (
            True,
            result.stdout +
            "\n" +
            "Requirements installed successfully."
        )


    except subprocess.TimeoutExpired:

        return (
            False,
            "requirements.txt installation timed out after 10 minutes."
        )


    except Exception as error:

        return (
            False,
            "Dependency installation error: " +
            repr(error)
        )


# ============================================================
# PROJECT COMMAND
# ============================================================

def get_project_command(project):

    folder = project_path(project)

    if not folder.exists():

        return None


    # --------------------------------------------------------
    # PYTHON
    # --------------------------------------------------------

    if project["kind"] == "Python":

        python_file = find_file(
            folder,
            {".py"},
            [
                "bot.py",
                "main.py",
                "app.py",
                "run.py"
            ]
        )

        if python_file:

            return [
                os.environ.get(
                    "PYTHON_BIN",
                    sys.executable
                ),
                str(python_file)
            ]


    # --------------------------------------------------------
    # NODE.JS
    # --------------------------------------------------------

    if project["kind"] == "Node.js":

        node_file = find_file(
            folder,
            {".js"},
            [
                "index.js",
                "server.js",
                "app.js",
                "bot.js",
                "main.js"
            ]
        )

        if node_file:

            return [
                "node",
                str(node_file)
            ]


    # --------------------------------------------------------
    # JAVA
    # --------------------------------------------------------

    if project["kind"] == "Java":

        jar_file = find_file(
            folder,
            {".jar"}
        )

        if jar_file:

            return [
                "java",
                "-jar",
                str(jar_file)
            ]


    # --------------------------------------------------------
    # PHP
    # --------------------------------------------------------

    if project["kind"] == "PHP":

        php_file = find_file(
            folder,
            {".php"},
            [
                "index.php",
                "app.php",
                "main.php"
            ]
        )

        if php_file:

            return [
                "php",
                str(php_file)
            ]


    return None


# ============================================================
# LOGGING
# ============================================================

def write_log(project_folder, message):

    log_file = (
        project_folder /
        "runtime.log"
    )

    with open(
        log_file,
        "a",
        encoding="utf-8"
    ) as log:

        log.write(
            "\n" +
            str(message) +
            "\n"
        )


def read_logs(project_folder):

    log_file = (
        project_folder /
        "runtime.log"
    )

    if not log_file.exists():

        return "No logs yet."


    try:

        return log_file.read_text(
            encoding="utf-8",
            errors="ignore"
        )[-20000:]

    except Exception:

        return "Unable to read logs."


# ============================================================
# PROCESS STATUS MONITOR
# ============================================================

def monitor_process(project_id, process):

    try:

        return_code = process.wait()

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

        RUNNING_PROCESSES.pop(
            project_id,
            None
        )

        log_file = PROCESS_LOG_FILES.get(
            project_id
        )

        if log_file:

            try:
                log_file.close()
            except Exception:
                pass

            PROCESS_LOG_FILES.pop(
                project_id,
                None
            )

        project_folder = PROJECTS_DIR / project_id

        if project_folder.exists():

            write_log(
                project_folder,
                f"\n[GodX] Process stopped. Exit code: {return_code}"
            )

    except Exception as error:

        project_folder = PROJECTS_DIR / project_id

        if project_folder.exists():

            write_log(
                project_folder,
                f"\n[GodX] Monitor error: {error!r}"
            )


# ============================================================
# HOME
# ============================================================

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


    # Refresh process statuses.
    refreshed = []

    for project in projects:

        project_dict = dict(project)

        process = RUNNING_PROCESSES.get(
            project["id"]
        )

        if (
            process
            and
            process.poll() is None
        ):

            project_dict["status"] = "running"

        else:

            project_dict["status"] = "stopped"


        refreshed.append(
            project_dict
        )


    return render_template(
        "dashboard.html",
        projects=refreshed
    )


# ============================================================
# REGISTER
# ============================================================

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


        password_hash = generate_password_hash(
            password
        )


        try:

            database = get_db()

            database.execute(
                """
                INSERT INTO users
                (
                    email,
                    password
                )
                VALUES (?, ?)
                """,
                (
                    email,
                    password_hash
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


# ============================================================
# LOGIN
# ============================================================

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


        # ----------------------------------------------------
        # ADMIN LOGIN
        # ----------------------------------------------------

        if (
            email == ADMIN_EMAIL
            and
            password == ADMIN_PASSWORD
        ):

            session.clear()

            session["admin"] = True

            return redirect("/admin")


        # ----------------------------------------------------
        # USER LOGIN
        # ----------------------------------------------------

        database = get_db()

        user = database.execute(
            """
            SELECT *
            FROM users
            WHERE email = ?
            """,
            (
                email,
            )
        ).fetchone()


        if user:

            stored_password = user["password"]

            valid_password = False

            try:

                valid_password = check_password_hash(
                    stored_password,
                    password
                )

            except Exception:

                # Support old accounts that were created
                # using the previous plain-text system.
                valid_password = (
                    stored_password == password
                )


            if valid_password:

                # Upgrade old plain-text password.
                if not stored_password.startswith(
                    "scrypt:"
                ) and not stored_password.startswith(
                    "pbkdf2:"
                ):

                    new_hash = generate_password_hash(
                        password
                    )

                    database.execute(
                        """
                        UPDATE users
                        SET password = ?
                        WHERE id = ?
                        """,
                        (
                            new_hash,
                            user["id"]
                        )
                    )

                    database.commit()


                database.close()

                session.clear()

                session["user_id"] = user["id"]

                return redirect("/")


        database.close()


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


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/login")


# ============================================================
# CREATE PROJECT
# ============================================================

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


    # --------------------------------------------------------
    # FREE PLAN LIMIT
    # --------------------------------------------------------

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
        ).strip()


        # ----------------------------------------------------
        # VALIDATE
        # ----------------------------------------------------

        if not project_name:

            return render_template(
                "message.html",
                message="Project name is required."
            )


        if project_type not in ALLOWED_PROJECT_TYPES:

            return render_template(
                "message.html",
                message="Invalid project type."
            )


        # ----------------------------------------------------
        # GET MULTIPLE FILES
        # ----------------------------------------------------

        uploaded_files = request.files.getlist(
            "files"
        )


        valid_files = [
            uploaded
            for uploaded in uploaded_files
            if uploaded
            and uploaded.filename
        ]


        if not valid_files:

            return render_template(
                "message.html",
                message="Please select at least one project file."
            )


        # ----------------------------------------------------
        # CREATE PROJECT FOLDER
        # ----------------------------------------------------

        project_id = uuid.uuid4().hex[:12]

        project_folder = (
            PROJECTS_DIR /
            project_id
        )

        project_folder.mkdir(
            parents=True,
            exist_ok=True
        )


        try:

            # ------------------------------------------------
            # SAVE ALL FILES
            # ------------------------------------------------

            for uploaded_file in valid_files:

                filename = secure_filename(
                    uploaded_file.filename
                )


                if not filename:

                    continue


                # Prevent files/directories that could
                # interfere with the hosting system.
                if filename in {
                    ".env",
                    "godx.db"
                }:

                    continue


                uploaded_file.save(
                    project_folder /
                    filename
                )


            # ------------------------------------------------
            # SAVE ENVIRONMENT VARIABLES
            # ------------------------------------------------

            env_keys = request.form.getlist(
                "env_key"
            )

            env_values = request.form.getlist(
                "env_value"
            )


            # ------------------------------------------------
            # DATABASE PROJECT
            # ------------------------------------------------

            database = get_db()

            database.execute(
                """
                INSERT INTO projects
                (
                    id,
                    user_id,
                    name,
                    kind,
                    folder,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    session["user_id"],
                    project_name,
                    project_type,
                    str(project_folder),
                    "stopped"
                )
            )


            database.commit()

            database.close()


            save_project_env(
                project_id,
                env_keys,
                env_values
            )


        except Exception as error:

            shutil.rmtree(
                project_folder,
                ignore_errors=True
            )

            return render_template(
                "message.html",
                message="Upload failed: " + str(error)
            )


        return redirect(
            "/project/" + project_id
        )


    return render_template(
        "create.html"
    )


# ============================================================
# PROJECT PAGE
# ============================================================

@app.route(
    "/project/<project_id>"
)
def project_page(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    project_folder = project_path(
        project
    )


    if not is_safe_project_path(
        project_folder
    ):

        abort(404)


    process = RUNNING_PROCESSES.get(
        project_id
    )


    if (
        process
        and
        process.poll() is None
    ):

        current_status = "running"

    else:

        current_status = "stopped"


    logs = read_logs(
        project_folder
    )


    files = [
        str(file.relative_to(project_folder))
        for file in safe_project_files(
            project_folder
        )
    ]


    env_variables = get_project_env(
        project_id
    )


    return render_template(
        "project.html",
        project=dict(
            project,
            status=current_status
        ),
        logs=logs,
        files=files,
        env_variables=env_variables
    )


# ============================================================
# START PROJECT
# ============================================================

@app.post(
    "/project/<project_id>/start"
)
def start_project(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    folder = project_path(
        project
    )


    if not is_safe_project_path(
        folder
    ):

        abort(404)


    # --------------------------------------------------------
    # ALREADY RUNNING
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # STATIC PROJECT
    # --------------------------------------------------------

    if project["kind"] == "Static":

        index_file = folder / "index.html"

        if not index_file.exists():

            return render_template(
                "message.html",
                message="Static project needs an index.html file."
            )


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


        return redirect(
            "/project/" + project_id
        )


    # --------------------------------------------------------
    # PYTHON REQUIREMENTS
    # --------------------------------------------------------

    if project["kind"] == "Python":

        success, dependency_log = (
            install_python_requirements(
                folder
            )
        )


        write_log(
            folder,
            "[GodX] " + dependency_log
        )


        if not success:

            return render_template(
                "message.html",
                message=(
                    "requirements.txt installation failed. "
                    "Check project logs."
                )
            )


    # --------------------------------------------------------
    # COMMAND
    # --------------------------------------------------------

    command = get_project_command(
        project
    )


    if not command:

        return render_template(
            "message.html",
            message=(
                "No runnable file found. "
                "For Python upload a .py file. "
                "For Node.js upload a .js file. "
                "For Java upload a .jar file. "
                "For PHP upload a .php file."
            )
        )


    # --------------------------------------------------------
    # ENVIRONMENT
    # --------------------------------------------------------

    environment = os.environ.copy()

    project_env = get_project_env(
        project_id
    )

    for key, value in project_env.items():

        environment[key] = value


    # For Python projects, make locally installed
    # dependencies available to the uploaded bot.
    if project["kind"] == "Python":

        deps_dir = folder / ".godx_deps"

        if deps_dir.exists():

            old_pythonpath = environment.get(
                "PYTHONPATH",
                ""
            )

            if old_pythonpath:

                environment["PYTHONPATH"] = (
                    str(deps_dir)
                    + os.pathsep
                    + old_pythonpath
                )

            else:

                environment["PYTHONPATH"] = str(
                    deps_dir
                )


    # --------------------------------------------------------
    # LOG FILE
    # --------------------------------------------------------

    log_file_path = (
        folder /
        "runtime.log"
    )


    log = open(
        log_file_path,
        "a",
        encoding="utf-8"
    )


    log.write(
        "\n\n[GodX] Starting project...\n"
    )

    log.write(
        "[GodX] Command: "
        +
        " ".join(command)
        +
        "\n"
    )

    log.flush()


    # --------------------------------------------------------
    # START PROCESS
    # --------------------------------------------------------

    try:

        process = subprocess.Popen(
            command,
            cwd=str(folder),
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )


        RUNNING_PROCESSES[
            project_id
        ] = process


        PROCESS_LOG_FILES[
            project_id
        ] = log


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


        monitor_thread = threading.Thread(
            target=monitor_process,
            args=(
                project_id,
                process
            ),
            daemon=True
        )

        monitor_thread.start()


    except Exception as error:

        log.write(
            "\n[GodX] START ERROR: "
            +
            repr(error)
            +
            "\n"
        )

        log.close()


        return render_template(
            "message.html",
            message="Unable to start project: " + str(error)
        )


    return redirect(
        "/project/" + project_id
    )


# ============================================================
# STOP PROJECT
# ============================================================

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


    log_file = PROCESS_LOG_FILES.pop(
        project_id,
        None
    )


    if log_file:

        try:

            log_file.close()

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


# ============================================================
# DELETE PROJECT
# ============================================================

@app.post(
    "/project/<project_id>/delete"
)
def delete_project(project_id):

    project = get_user_project(
        project_id
    )


    if not project:

        abort(404)


    # --------------------------------------------------------
    # STOP PROCESS
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # CLOSE LOG
    # --------------------------------------------------------

    log_file = PROCESS_LOG_FILES.pop(
        project_id,
        None
    )


    if log_file:

        try:

            log_file.close()

        except Exception:

            pass


    # --------------------------------------------------------
    # DELETE FILES
    # --------------------------------------------------------

    project_folder = project_path(
        project
    )


    if (
        is_safe_project_path(
            project_folder
        )
        and
        project_folder.exists()
    ):

        shutil.rmtree(
            project_folder,
            ignore_errors=True
        )


    # --------------------------------------------------------
    # DELETE ENV + PROJECT
    # --------------------------------------------------------

    database = get_db()

    database.execute(
        """
        DELETE FROM project_env
        WHERE project_id = ?
        """,
        (
            project_id,
        )
    )


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


# ============================================================
# STATIC WEBSITE
# ============================================================

@app.route(
    "/site/<project_id>/",
    defaults={"file_path": "index.html"}
)
@app.route(
    "/site/<project_id>/<path:file_path>"
)
def static_project(project_id, file_path):

    database = get_db()

    project = database.execute(
        """
        SELECT *
        FROM projects
        WHERE id = ?
        """,
        (
            project_id,
        )
    ).fetchone()

    database.close()


    if not project:

        abort(404)


    if project["kind"] != "Static":

        abort(404)


    folder = project_path(
        project
    )


    if not is_safe_project_path(
        folder
    ):

        abort(404)


    requested = (
        folder /
        file_path
    ).resolve()


    if not is_safe_project_path(
        requested
    ):

        abort(404)


    if not requested.exists():

        abort(404)


    if not requested.is_file():

        abort(404)


    relative_folder = requested.parent.relative_to(
        folder
    )


    return send_from_directory(
        folder / relative_folder,
        requested.name
    )


# ============================================================
# ADMIN PANEL
# ============================================================

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


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def file_too_large(error):

    return render_template(
        "message.html",
        message=(
            f"Upload is too large. "
            f"Maximum allowed size is {MAX_UPLOAD_MB} MB."
        )
    ), 413


# ============================================================
# RAILWAY START
# ============================================================

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
