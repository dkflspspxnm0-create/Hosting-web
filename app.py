import os
import sys
import sqlite3
import uuid
import signal
import shutil
import subprocess
import threading
import shlex
from pathlib import Path

from flask import (
    Flask,
    request,
    redirect,
    session,
    render_template,
    abort,
    send_from_directory
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from werkzeug.utils import secure_filename


# ============================================================
# GODX HOSTING
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DATABASE = DATA_DIR / "godx.db"

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True
)

PROJECTS_DIR.mkdir(
    parents=True,
    exist_ok=True
)


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "CHANGE_THIS_SECRET_KEY"
)


# ============================================================
# UPLOAD LIMIT
# ============================================================

try:
    MAX_UPLOAD_MB = int(
        os.environ.get(
            "MAX_UPLOAD_MB",
            "500"
        )
    )
except ValueError:
    MAX_UPLOAD_MB = 500


app.config["MAX_CONTENT_LENGTH"] = (
    MAX_UPLOAD_MB * 1024 * 1024
)


# ============================================================
# ADMIN
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
# PROCESS STORAGE
# ============================================================

RUNNING_PROCESSES = {}

PROCESS_LOG_FILES = {}

PROCESS_LOCK = threading.Lock()


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


init_database()


# ============================================================
# CONSTANTS
# ============================================================

ALLOWED_PROJECT_TYPES = {
    "Python",
    "Node.js",
    "Java",
    "PHP",
    "Static"
}


# ============================================================
# AUTH HELPERS
# ============================================================

def is_logged_in():

    return bool(session.get("user_id"))


def is_admin():

    return bool(session.get("admin"))


# ============================================================
# PROJECT HELPERS
# ============================================================

def project_path(project):

    return Path(
        project["folder"]
    ).resolve()


def is_safe_project_path(path):

    try:

        path.resolve().relative_to(
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


def get_project_by_id(project_id):

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

    return project


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

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


def save_project_env(
    project_id,
    keys,
    values
):

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

    for key, value in zip(
        keys,
        values
    ):

        key = (
            key or ""
        ).strip()

        value = (
            value or ""
        )

        if not key:
            continue

        # Basic environment variable validation.
        if not key.replace(
            "_",
            ""
        ).isalnum():

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


# ============================================================
# FILE HELPERS
# ============================================================

def safe_project_files(
    project_folder
):

    files = []

    if not project_folder.exists():

        return files

    for item in project_folder.rglob("*"):

        if not item.is_file():
            continue

        if item.name == "runtime.log":
            continue

        # Don't show generated dependencies.
        if ".godx_deps" in item.parts:
            continue

        try:

            relative = item.relative_to(
                project_folder
            )

            files.append(relative)

        except ValueError:

            pass

    return files


def find_file(
    folder,
    suffixes,
    preferred_names=None
):

    preferred_names = (
        preferred_names or []
    )

    files = safe_project_files(
        folder
    )

    # Preferred files first.
    for preferred in preferred_names:

        for relative in files:

            if (
                relative.name.lower()
                ==
                preferred.lower()
            ):

                return folder / relative

    # Then extension.
    for relative in files:

        if (
            relative.suffix.lower()
            in suffixes
        ):

            return folder / relative

    return None


# ============================================================
# PYTHON REQUIREMENTS
# ============================================================

def install_python_requirements(
    folder
):

    requirements_file = (
        folder / "requirements.txt"
    )

    # requirements.txt is OPTIONAL.
    if not requirements_file.exists():

        return (
            True,
            "No requirements.txt found. "
            "Skipping dependency installation."
        )

    deps_dir = (
        folder / ".godx_deps"
    )

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

        output = (
            result.stdout
            + "\n"
            + result.stderr
        )

        if result.returncode != 0:

            return (
                False,
                output
            )

        return (
            True,
            output
            + "\n"
            + "Requirements installed successfully."
        )

    except subprocess.TimeoutExpired:

        return (
            False,
            "requirements.txt installation "
            "timed out after 10 minutes."
        )

    except Exception as error:

        return (
            False,
            "Dependency installation error: "
            + repr(error)
        )


# ============================================================
# PROCFILE
# ============================================================

def get_procfile_command(
    folder
):

    procfile = (
        folder / "Procfile"
    )

    if not procfile.exists():

        return None

    try:

        lines = procfile.read_text(
            encoding="utf-8",
            errors="ignore"
        ).splitlines()

    except Exception:

        return None

    for line in lines:

        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        # Standard:
        # web: python bot.py
        if ":" in line:

            process_type, command = (
                line.split(
                    ":",
                    1
                )
            )

            if (
                process_type.strip()
                in {
                    "web",
                    "worker"
                }
            ):

                command = command.strip()

                if command:

                    try:

                        return shlex.split(
                            command
                        )

                    except ValueError:

                        return None

        # Also allow:
        # python bot.py
        try:

            return shlex.split(
                line
            )

        except ValueError:

            return None

    return None


# ============================================================
# PROJECT COMMAND
# ============================================================

def get_project_command(
    project
):

    folder = project_path(
        project
    )

    if not folder.exists():

        return None

    # --------------------------------------------------------
    # PROCFILE
    # --------------------------------------------------------

    procfile_command = (
        get_procfile_command(
            folder
        )
    )

    if procfile_command:

        return procfile_command

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

    # --------------------------------------------------------
    # STATIC
    # --------------------------------------------------------

    if project["kind"] == "Static":

        return None

    return None


# ============================================================
# LOGGING
# ============================================================

def write_log(
    project_folder,
    message
):

    log_file = (
        project_folder /
        "runtime.log"
    )

    try:

        with open(
            log_file,
            "a",
            encoding="utf-8"
        ) as log:

            log.write(
                "\n"
                + str(message)
                + "\n"
            )

    except Exception:
        pass


def read_logs(
    project_folder
):

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
# PROCESS MONITOR
# ============================================================

def monitor_process(
    project_id,
    process
):

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

        with PROCESS_LOCK:

            RUNNING_PROCESSES.pop(
                project_id,
                None
            )

            log_file = (
                PROCESS_LOG_FILES.pop(
                    project_id,
                    None
                )
            )

        if log_file:

            try:
                log_file.close()
            except Exception:
                pass

        project_folder = (
            PROJECTS_DIR /
            project_id
        )

        if project_folder.exists():

            write_log(
                project_folder,
                "[GodX] Process stopped. "
                f"Exit code: {return_code}"
            )

    except Exception as error:

        project_folder = (
            PROJECTS_DIR /
            project_id
        )

        if project_folder.exists():

            write_log(
                project_folder,
                "[GodX] Monitor error: "
                + repr(error)
            )


# ============================================================
# HOME / DASHBOARD
# ============================================================

@app.route("/")
def home():

    if is_admin():

        return redirect(
            "/admin"
        )

    if not is_logged_in():

        return redirect(
            "/login"
        )

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

    refreshed = []

    for project in projects:

        project_dict = dict(
            project
        )

        process = (
            RUNNING_PROCESSES.get(
                project["id"]
            )
        )

        if (
            process
            and
            process.poll() is None
        ):

            project_dict[
                "status"
            ] = "running"

        else:

            project_dict[
                "status"
            ] = "stopped"

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
                error=(
                    "Email and password "
                    "are required."
                )
            )

        if len(password) < 6:

            return render_template(
                "auth.html",
                mode="register",
                error=(
                    "Password must be at "
                    "least 6 characters."
                )
            )

        password_hash = (
            generate_password_hash(
                password
            )
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

            session["user_id"] = (
                user["id"]
            )

            return redirect("/")

        except sqlite3.IntegrityError:

            try:
                database.close()
            except Exception:
                pass

            return render_template(
                "auth.html",
                mode="register",
                error=(
                    "This email is "
                    "already registered."
                )
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
        # ADMIN
        # ----------------------------------------------------

        if (
            email == ADMIN_EMAIL
            and
            password == ADMIN_PASSWORD
        ):

            session.clear()

            session["admin"] = True

            return redirect(
                "/admin"
            )

        # ----------------------------------------------------
        # USER
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

            stored_password = (
                user["password"]
            )

            valid_password = False

            try:

                valid_password = (
                    check_password_hash(
                        stored_password,
                        password
                    )
                )

            except Exception:

                # Compatibility with old
                # plaintext accounts.
                valid_password = (
                    stored_password
                    ==
                    password
                )

            if valid_password:

                # Upgrade old plaintext password.
                if not (
                    stored_password.startswith(
                        "scrypt:"
                    )
                    or
                    stored_password.startswith(
                        "pbkdf2:"
                    )
                ):

                    new_hash = (
                        generate_password_hash(
                            password
                        )
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

                session["user_id"] = (
                    user["id"]
                )

                return redirect("/")

        database.close()

        return render_template(
            "auth.html",
            mode="login",
            error=(
                "Invalid email or "
                "password."
            )
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

    return redirect(
        "/login"
    )


# ============================================================
# CREATE PROJECT
# ============================================================

@app.route(
    "/create",
    methods=["GET", "POST"]
)
def create_project():

    if not is_logged_in():

        return redirect(
            "/login"
        )

    # --------------------------------------------------------
    # FREE PLAN LIMIT
    # --------------------------------------------------------

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

    if count >= 2:

        return render_template(
            "message.html",
            message=(
                "Free plan limit: "
                "2 projects."
            )
        )

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    if request.method == "GET":

        return render_template(
            "create.html"
        )

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    project_name = request.form.get(
        "name",
        ""
    ).strip()

    project_type = request.form.get(
        "kind",
        ""
    ).strip()

    # Latest create.html uses:
    # name="files"
    uploaded_files = request.files.getlist(
        "files"
    )

    # --------------------------------------------------------
    # VALIDATION
    # --------------------------------------------------------

    if not project_name:

        return render_template(
            "message.html",
            message=(
                "Project name is required."
            )
        )

    if len(project_name) > 100:

        return render_template(
            "message.html",
            message=(
                "Project name is too long."
            )
        )

    if project_type not in ALLOWED_PROJECT_TYPES:

        return render_template(
            "message.html",
            message=(
                "Invalid project type."
            )
        )

    valid_files = []

    for uploaded_file in uploaded_files:

        if not uploaded_file:
            continue

        if not uploaded_file.filename:
            continue

        filename = secure_filename(
            uploaded_file.filename
        )

        if not filename:
            continue

        valid_files.append(
            (
                uploaded_file,
                filename
            )
        )

    if not valid_files:

        return render_template(
            "message.html",
            message=(
                "Please select at least "
                "one file."
            )
        )

    # --------------------------------------------------------
    # CREATE PROJECT
    # --------------------------------------------------------

    project_id = uuid.uuid4().hex

    folder = (
        PROJECTS_DIR /
        project_id
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    try:

        # ----------------------------------------------------
        # SAVE ALL FILES
        # ----------------------------------------------------

        for uploaded_file, filename in valid_files:

            destination = (
                folder /
                filename
            ).resolve()

            if not is_safe_project_path(
                destination
            ):

                raise ValueError(
                    "Unsafe file path."
                )

            uploaded_file.save(
                destination
            )

        # ----------------------------------------------------
        # ENV VARIABLES
        # ----------------------------------------------------

        env_keys = request.form.getlist(
            "env_key"
        )

        env_values = request.form.getlist(
            "env_value"
        )

        # ----------------------------------------------------
        # DATABASE
        # ----------------------------------------------------

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
                str(folder),
                "stopped"
            )
        )

        database.commit()

        database.close()

        # Save environment variables.
        save_project_env(
            project_id,
            env_keys,
            env_values
        )

        write_log(
            folder,
            "[GodX] Project created."
        )

        write_log(
            folder,
            "[GodX] Project type: "
            + project_type
        )

        write_log(
            folder,
            "[GodX] Files uploaded: "
            + str(len(valid_files))
        )

        return redirect(
            "/project/"
            + project_id
        )

    except Exception as error:

        # Cleanup if project creation fails.
        try:

            if folder.exists():

                shutil.rmtree(
                    folder,
                    ignore_errors=True
                )

        except Exception:
            pass

        return render_template(
            "message.html",
            message=(
                "Project creation failed: "
                + repr(error)
            )
        )


# ============================================================
# PROJECT PAGE
# ============================================================

@app.route(
    "/project/<project_id>"
)
def project_page(
    project_id
):

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

        abort(403)

    files = [
        str(path)
        for path in safe_project_files(
            folder
        )
    ]

    logs = read_logs(
        folder
    )

    env = get_project_env(
        project_id
    )

    process = (
        RUNNING_PROCESSES.get(
            project_id
        )
    )

    if (
        process
        and
        process.poll() is None
    ):

        status = "running"

    else:

        status = "stopped"

    return render_template(
        "project.html",
        project=project,
        files=files,
        logs=logs,
        env=env,
        status=status
    )


# ============================================================
# PROJECT FILE VIEW
# ============================================================

@app.route(
    "/project/<project_id>/file/<path:filename>"
)
def project_file(
    project_id,
    filename
):

    project = get_user_project(
        project_id
    )

    if not project:

        abort(404)

    folder = project_path(
        project
    )

    requested = (
        folder /
        filename
    ).resolve()

    if not is_safe_project_path(
        requested
    ):

        abort(403)

    if not requested.is_file():

        abort(404)

    return send_from_directory(
        folder,
        filename
    )


# ============================================================
# START PROJECT
# ============================================================

@app.route(
    "/project/<project_id>/start",
    methods=["POST"]
)
def start_project(
    project_id
):

    project = get_user_project(
        project_id
    )

    if not project:

        abort(404)

    folder = project_path(
        project
    )

    if not folder.exists():

        return render_template(
            "message.html",
            message=(
                "Project folder not found."
            )
        )

    # Already running?
    existing = (
        RUNNING_PROCESSES.get(
            project_id
        )
    )

    if (
        existing
        and
        existing.poll() is None
    ):

        return redirect(
            "/project/"
            + project_id
        )

    # --------------------------------------------------------
    # STATIC PROJECT
    # --------------------------------------------------------

    if project["kind"] == "Static":

        return render_template(
            "message.html",
            message=(
                "Static projects do not "
                "have a process to start "
                "in this version."
            )
        )

    # --------------------------------------------------------
    # PYTHON REQUIREMENTS
    # --------------------------------------------------------

    if project["kind"] == "Python":

        write_log(
            folder,
            "[GodX] Checking requirements.txt..."
        )

        success, output = (
            install_python_requirements(
                folder
            )
        )

        write_log(
            folder,
            output
        )

        if not success:

            return render_template(
                "message.html",
                message=(
                    "Dependency installation "
                    "failed. Check project logs."
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
                "No runnable file found "
                "for this project."
            )
        )

    # --------------------------------------------------------
    # ENVIRONMENT
    # --------------------------------------------------------

    project_env = get_project_env(
        project_id
    )

    process_env = os.environ.copy()

    process_env.update(
        project_env
    )

    # Python dependency path.
    deps_dir = (
        folder /
        ".godx_deps"
    )

    if deps_dir.exists():

        old_python_path = (
            process_env.get(
                "PYTHONPATH",
                ""
            )
        )

        if old_python_path:

            process_env[
                "PYTHONPATH"
            ] = (
                str(deps_dir)
                + os.pathsep
                + old_python_path
            )

        else:

            process_env[
                "PYTHONPATH"
            ] = str(
                deps_dir
            )

    # --------------------------------------------------------
    # LOG FILE
    # --------------------------------------------------------

    log_path = (
        folder /
        "runtime.log"
    )

    try:

        log_file = open(
            log_path,
            "a",
            encoding="utf-8"
        )

    except Exception as error:

        return render_template(
            "message.html",
            message=(
                "Unable to open log file: "
                + repr(error)
            )
        )

    write_log(
        folder,
        "[GodX] Starting project..."
    )

    write_log(
        folder,
        "[GodX] Command: "
        + " ".join(command)
    )

    try:

        process = subprocess.Popen(
            command,
            cwd=str(folder),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=process_env
        )

    except FileNotFoundError as error:

        try:
            log_file.close()
        except Exception:
            pass

        write_log(
            folder,
            "[GodX] Runtime not found: "
            + repr(error)
        )

        return render_template(
            "message.html",
            message=(
                "Required runtime is not "
                "installed on the server."
            )
        )

    except Exception as error:

        try:
            log_file.close()
        except Exception:
            pass

        write_log(
            folder,
            "[GodX] Start error: "
            + repr(error)
        )

        return render_template(
            "message.html",
            message=(
                "Project could not start: "
                + repr(error)
            )
        )

    with PROCESS_LOCK:

        RUNNING_PROCESSES[
            project_id
        ] = process

        PROCESS_LOG_FILES[
            project_id
        ] = log_file

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

    write_log(
        folder,
        "[GodX] Project started. "
        f"PID: {process.pid}"
    )

    monitor_thread = threading.Thread(
        target=monitor_process,
        args=(
            project_id,
            process
        ),
        daemon=True
    )

    monitor_thread.start()

    return redirect(
        "/project/"
        + project_id
    )


# ============================================================
# STOP PROJECT
# ============================================================

@app.route(
    "/project/<project_id>/stop",
    methods=["POST"]
)
def stop_project(
    project_id
):

    project = get_user_project(
        project_id
    )

    if not project:

        abort(404)

    process = (
        RUNNING_PROCESSES.get(
            project_id
        )
    )

    if not process:

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
            "/project/"
            + project_id
        )

    try:

        write_log(
            project_path(project),
            "[GodX] Stopping project..."
        )

        process.terminate()

        try:

            process.wait(
                timeout=10
            )

        except subprocess.TimeoutExpired:

            process.kill()

    except Exception as error:

        write_log(
            project_path(project),
            "[GodX] Stop error: "
            + repr(error)
        )

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

    with PROCESS_LOCK:

        RUNNING_PROCESSES.pop(
            project_id,
            None
        )

        log_file = (
            PROCESS_LOG_FILES.pop(
                project_id,
                None
            )
        )

    if log_file:

        try:
            log_file.close()
        except Exception:
            pass

    return redirect(
        "/project/"
        + project_id
    )


# ============================================================
# RESTART PROJECT
# ============================================================

@app.route(
    "/project/<project_id>/restart",
    methods=["POST"]
)
def restart_project(
    project_id
):

    project = get_user_project(
        project_id
    )

    if not project:

        abort(404)

    process = (
        RUNNING_PROCESSES.get(
            project_id
        )
    )

    if process:

        try:

            process.terminate()

            try:

                process.wait(
                    timeout=10
                )

            except subprocess.TimeoutExpired:

                process.kill()

        except Exception:
            pass

        with PROCESS_LOCK:

            RUNNING_PROCESSES.pop(
                project_id,
                None
            )

            log_file = (
                PROCESS_LOG_FILES.pop(
                    project_id,
                    None
                )
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
        "/project/"
        + project_id
        + "?restart=1"
    )


# ============================================================
# RESTART HANDLER
# ============================================================

@app.route(
    "/project/<project_id>/restart-run",
    methods=["POST"]
)
def restart_run_project(
    project_id
):

    project = get_user_project(
        project_id
    )

    if not project:

        abort(404)

    # Stop first.
    process = (
        RUNNING_PROCESSES.get(
            project_id
        )
    )

    if process:

        try:
            process.terminate()
            process.wait(
                timeout=10
            )
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    with PROCESS_LOCK:

        RUNNING_PROCESSES.pop(
            project_id,
            None
        )

        log_file = (
            PROCESS_LOG_FILES.pop(
                project_id,
                None
            )
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

    # Start again internally.
    return redirect(
        "/project/"
        + project_id
    )


# ============================================================
# DELETE PROJECT
# ============================================================

@app.route(
    "/project/<project_id>/delete",
    methods=["POST"]
)
def delete_project(
    project_id
):

    project = get_user_project(
        project_id
    )

    if not project:

        abort(404)

    # Stop running process first.
    process = (
        RUNNING_PROCESSES.get(
            project_id
        )
    )

    if process:

        try:

            process.terminate()

            try:

                process.wait(
                    timeout=10
                )

            except subprocess.TimeoutExpired:

                process.kill()

        except Exception:
            pass

    with PROCESS_LOCK:

        RUNNING_PROCESSES.pop(
            project_id,
            None
        )

        log_file = (
            PROCESS_LOG_FILES.pop(
                project_id,
                None
            )
        )

    if log_file:

        try:
            log_file.close()
        except Exception:
            pass

    folder = project_path(
        project
    )

    if not is_safe_project_path(
        folder
    ):

        abort(403)

    # Delete files.
    if folder.exists():

        shutil.rmtree(
            folder,
            ignore_errors=True
        )

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
        AND user_id = ?
        """,
        (
            project_id,
            session["user_id"]
        )
    )

    database.commit()
    database.close()

    return redirect("/")


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
def admin():

    if not is_admin():

        return redirect(
            "/login"
        )

    database = get_db()

    users_count = database.execute(
        """
        SELECT COUNT(*) AS total
        FROM users
        """
    ).fetchone()["total"]

    projects_count = database.execute(
        """
        SELECT COUNT(*) AS total
        FROM projects
        """
    ).fetchone()["total"]

    running_count = database.execute(
        """
        SELECT COUNT(*) AS total
        FROM projects
        WHERE status = 'running'
        """
    ).fetchone()["total"]

    users = database.execute(
        """
        SELECT
            id,
            email
        FROM users
        ORDER BY id DESC
        """
    ).fetchall()

    projects = database.execute(
        """
        SELECT
            id,
            user_id,
            name,
            kind,
            status,
            created_at
        FROM projects
        ORDER BY created_at DESC
        """
    ).fetchall()

    database.close()

    return render_template(
        "admin.html",
        users_count=users_count,
        projects_count=projects_count,
        running_count=running_count,
        users=users,
        projects=projects
    )


# ============================================================
# ADMIN LOGOUT
# ============================================================

@app.route("/admin/logout")
def admin_logout():

    session.clear()

    return redirect(
        "/login"
    )


# ============================================================
# 413 - FILE TOO LARGE
# ============================================================

@app.errorhandler(413)
def file_too_large(error):

    return render_template(
        "message.html",
        message=(
            f"Upload too large. "
            f"Maximum allowed size is "
            f"{MAX_UPLOAD_MB} MB."
        )
    ), 413


# ============================================================
# 404
# ============================================================

@app.errorhandler(404)
def not_found(error):

    return render_template(
        "message.html",
        message="Page not found."
    ), 404


# ============================================================
# 500
# ============================================================

@app.errorhandler(500)
def server_error(error):

    return render_template(
        "message.html",
        message=(
            "Internal server error. "
            "Check Railway logs."
        )
    ), 500


# ============================================================
# RUN
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
