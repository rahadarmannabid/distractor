from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError
import os
from pathlib import Path
from dotenv import load_dotenv

# Load project .env first; override=True so repo config wins over stale shell/IDE env (e.g. old FIREBASE_*).
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)
from openai import OpenAI
import re
import keyword
import subprocess
import sys
import tempfile
import time
import uuid
import json
import mimetypes 
from datetime import datetime, timezone
from typing import List, Dict, Optional
import firebase_admin
from firebase_admin import credentials, db
from fastapi.responses import HTMLResponse, RedirectResponse
import asyncio
import shlex
from typing import List, Dict, Optional, Any
import smtplib
from email.message import EmailMessage
import traceback
from typing import List, Dict, Optional, Any
from enum import Enum
import json
from fastapi.responses import FileResponse
from admin import router as admin_router
from instructor import router as instructor_router
from firebase_utils import fb_dict

import logging
import html as html_std

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Instructor quiz problem fields may contain HTML from the rich-text editor (Quill).
_HTML_TAG_RE = re.compile(r"<[a-zA-Z][\w:-]*(?:\s[^>]*)?>")


def _field_looks_like_html(s: str) -> bool:
    return bool(_HTML_TAG_RE.search(s))


def sanitize_instructor_rich_html(raw: Optional[str]) -> str:
    """Strip unsafe tags/scripts; allow Quill-like formatting (bold, color, hr, lists, links)."""
    if raw is None or not str(raw).strip():
        return ""
    try:
        import bleach
        from bleach.css_sanitizer import CSSSanitizer
    except ImportError:
        logger.warning("bleach not installed; escaping problem HTML instead")
        return html_std.escape(str(raw))
    css_sanitizer = CSSSanitizer(
        allowed_css_properties=[
            "color",
            "background-color",
            "text-align",
            "font-weight",
            "font-style",
            "text-decoration",
            "margin",
            "margin-left",
            "padding",
            "white-space",
        ]
    )
    allowed_tags = [
        "p",
        "br",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "strike",
        "sub",
        "sup",
        "h1",
        "h2",
        "h3",
        "h4",
        "ul",
        "ol",
        "li",
        "a",
        "blockquote",
        "pre",
        "code",
        "hr",
        "span",
        "div",
    ]
    allowed_attrs = {
        "a": ["href", "title", "target", "rel"],
        "p": ["class", "style"],
        "span": ["class", "style"],
        "div": ["class", "style"],
        "ol": ["class", "style"],
        "ul": ["class", "style"],
        "li": ["class", "style"],
        "h1": ["class", "style"],
        "h2": ["class", "style"],
        "h3": ["class", "style"],
        "h4": ["class", "style"],
        "blockquote": ["class", "style"],
        "pre": ["class", "style", "spellcheck"],
        "code": ["class", "style"],
    }
    return bleach.clean(
        str(raw),
        tags=allowed_tags,
        attributes=allowed_attrs,
        css_sanitizer=css_sanitizer,
        strip=True,
    )


def format_instructor_problem_html_field(raw: Optional[str]) -> tuple[str, bool]:
    """
    Returns (safe_html_fragment, is_rich_html).
    Legacy plain text is escaped; HTML from the instructor editor is sanitized.
    """
    if raw is None or not str(raw).strip():
        return "", False
    s = str(raw)
    if not _field_looks_like_html(s):
        return html_std.escape(s), False
    return sanitize_instructor_rich_html(s), True
def initialize_firebase():
    """Initialize Firebase Admin SDK using environment variables"""
    try:
        if not firebase_admin._apps:
            # Try to initialize from individual environment variables first
            private_key = os.getenv("FIREBASE_PRIVATE_KEY")
            if private_key and all(os.getenv(var) for var in [
                "FIREBASE_PROJECT_ID", "FIREBASE_PRIVATE_KEY_ID",
                "FIREBASE_CLIENT_EMAIL", "FIREBASE_CLIENT_ID"
            ]):
                cred_dict = {
                    "type": "service_account",
                    "project_id": os.getenv("FIREBASE_PROJECT_ID"),
                    "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
                    "private_key": private_key.replace("\\n", "\n"),
                    "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
                    "client_id": os.getenv("FIREBASE_CLIENT_ID"),
                    "auth_uri": os.getenv("FIREBASE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
                    "token_uri": os.getenv("FIREBASE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
                    "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
                    "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_CERT_URL"),
                    "universe_domain": os.getenv("FIREBASE_UNIVERSE_DOMAIN", "googleapis.com")
                }
                
                cred = credentials.Certificate(cred_dict)
                database_url = os.getenv("FIREBASE_DATABASE_URL")
                if database_url:
                    firebase_admin.initialize_app(cred, {
                        'databaseURL': database_url
                    })
                    logger.info(
                        "Firebase Realtime Database initialized: project_id=%s databaseURL=%s",
                        os.getenv("FIREBASE_PROJECT_ID"),
                        database_url,
                    )
                    return db.reference()
                else:
                    logger.warning("FIREBASE_DATABASE_URL not found in environment variables")
                    return None
                
            # Fallback to service account file if available
            elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                if cred_path and os.path.exists(cred_path):
                    cred = credentials.Certificate(cred_path)
                    database_url = os.getenv("FIREBASE_DATABASE_URL")
                    if database_url:
                        firebase_admin.initialize_app(cred, {
                            'databaseURL': database_url
                        })
                        logger.info(
                            "Firebase initialized (GOOGLE_APPLICATION_CREDENTIALS): databaseURL=%s",
                            database_url,
                        )
                        return db.reference()
                    else:
                        logger.warning("FIREBASE_DATABASE_URL not found in environment variables")
                        return None
                else:
                    logger.warning(f"Firebase credentials file not found at: {cred_path}")
                    return None
            else:
                logger.warning("Firebase credentials not found - running without Firebase")
                return None
        else:
            # Firebase already initialized, return existing reference
            return db.reference()
                
    except Exception as e:
        logger.error(f"Firebase initialization failed: {e}")
        return None
firebase_db = initialize_firebase()

def get_condition_keys():
    """Get condition keys from environment variables"""
    condition_1_keys = os.getenv("CONDITION_1_PREFIXES", "Condition1_all").split(",")
    condition_2_keys = os.getenv("CONDITION_2_PREFIXES", "Condition2_partial,fdasdfsa").split(",")
    condition_3_keys = os.getenv("CONDITION_3_PREFIXES", "Condition3_suggestions").split(",")
    
    # Clean up the keys (remove spaces and empty strings)
    condition_1_keys = [key.strip() for key in condition_1_keys if key.strip()]
    condition_2_keys = [key.strip() for key in condition_2_keys if key.strip()]
    condition_3_keys = [key.strip() for key in condition_3_keys if key.strip()]
    
    return condition_1_keys, condition_2_keys, condition_3_keys


def get_final_survey_disabled_prefixes():
    """Get prefixes that should NOT show final periodic survey"""
    disabled_prefixes = os.getenv("FINAL_SURVEY", "").lower().split(",")
    # Clean up the prefixes (remove spaces and empty strings)
    disabled_prefixes = [prefix.strip() for prefix in disabled_prefixes if prefix.strip()]
    return disabled_prefixes


# Starter code directory
STARTER_CODE_DIR = os.getenv("STARTER_CODE_DIR", "starter_code")

def get_starter_code(prefix, language):
    """
    Get starter code for a given prefix and programming language
    Returns None if file not found
    """
    if not prefix or not language:
        return None
    
    # Map language to file extension
    language_extensions = {
        'javascript': 'js',
        'python': 'py',
        'c': 'c'
    }
    
    extension = language_extensions.get(language.lower())
    if not extension:
        logger.warning(f"Unknown language: {language}")
        return None
    
    # Construct file path (case-insensitive prefix matching)
    starter_code_path = Path(STARTER_CODE_DIR)
    
    if not starter_code_path.exists():
        logger.warning(f"Starter code directory does not exist: {starter_code_path}")
        return None
    
    # Try exact match first
    file_path = starter_code_path / f"{prefix}.{extension}"
    
    if file_path.exists():
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                logger.info(f"Loaded starter code from: {file_path}")
                return content
        except Exception as e:
            logger.error(f"Error reading starter code file {file_path}: {e}")
            return None
    
    # Try case-insensitive match
    try:
        for file in starter_code_path.iterdir():
            if file.is_file() and file.stem.lower() == prefix.lower() and file.suffix == f".{extension}":
                with open(file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    logger.info(f"Loaded starter code from: {file} (case-insensitive match)")
                    return content
    except Exception as e:
        logger.error(f"Error searching for starter code: {e}")
    
    logger.info(f"No starter code found for prefix '{prefix}' with language '{language}'")
    return None


def get_side_panel_hidden_assignment_keys():
    """Get assignment keys that should have sidebar hidden by default"""
    hidden_keys = os.getenv("SIDE_PANEL_HIDDEN", "").lower().split(",")
    # Clean up the assignment keys (remove spaces and empty strings)
    hidden_keys = [key.strip() for key in hidden_keys if key.strip()]
    # Return as comma-separated string for template
    result = ",".join(hidden_keys)
    logger.info(f"Sidebar hidden for assignment keys: {result}")
    return result

def get_periodic_survey_config():
    """Get periodic survey configuration from environment variables"""
    periodic_prefixes = os.getenv("PERIODIC_SURVEY", "").lower().split(",")
    periodic_prefixes = [prefix.strip() for prefix in periodic_prefixes if prefix.strip()]
    
    start_after = os.getenv("PERIODIC_SURVEY_START", "5")
    try:
        start_after = int(start_after)
    except ValueError:
        start_after = 5  # Default to 5 if invalid
    
    return {
        "enabled_prefixes": periodic_prefixes,
        "start_after_suggestions": start_after
    }

def is_final_survey_disabled(prefix):
    """Check if final periodic survey is disabled for this prefix"""
    if not prefix:
        return False
    
    disabled_prefixes = get_final_survey_disabled_prefixes()
    prefix_lower = prefix.lower()
    
    # Check if prefix matches any disabled prefix
    return any(prefix_lower.startswith(disabled.lower()) for disabled in disabled_prefixes)

def get_assignment_base_key(assignment_key):
    """
    Extract the base key from assignment key for file reading
    Returns the base prefix that matches the assignment key
    CASE-INSENSITIVE: 'proteus', 'Proteus', 'PROTEUS' all return 'Proteus'
    """
    if not assignment_key:
        return None
    
    condition_1_keys, condition_2_keys, condition_3_keys = get_condition_keys()
    all_keys = condition_1_keys + condition_2_keys + condition_3_keys
    # Convert to lowercase for case-insensitive comparison
    assignment_key_lower = assignment_key.strip().lower()
    
    # Convert all keys to lowercase for comparison
    all_keys_lower = [key.lower() for key in all_keys]
    
    # Check for exact match first
    if assignment_key_lower in all_keys_lower:
        # Return with capital first letter to match filename (Proteus.json)
        return assignment_key_lower.capitalize()
    
    # Check for prefix match and return the matching base key with capital first letter
    for base_key in all_keys_lower:
        if assignment_key_lower.startswith(base_key):
            return base_key.capitalize()
    
    return None

def get_assignment_condition(assignment_key):
    """
    Determine which condition the assignment key belongs to
    Returns: 1 for full UI, 2 for partial UI (guess only), 3 for suggestions only, None for invalid
    EXACT MATCH ONLY: Case-insensitive and whitespace-trimmed, but no prefix/suffix variations allowed
    Examples:
      - "pattern", "Pattern", " pattern " → Valid (exact match)
      - "pattern_groupA", "pattern123" → Invalid (not exact match)
    """
    if not assignment_key:
        return None
    
    condition_1_keys, condition_2_keys, condition_3_keys = get_condition_keys()
    
    # Normalize: trim whitespace and convert to lowercase
    assignment_key = assignment_key.strip().lower()
    
    # Convert all condition keys to lowercase for comparison
    condition_1_keys = [key.lower() for key in condition_1_keys]
    condition_2_keys = [key.lower() for key in condition_2_keys]
    condition_3_keys = [key.lower() for key in condition_3_keys]
    
    # EXACT MATCH ONLY - No prefix matching
    if assignment_key in condition_1_keys:
        return 1
    
    if assignment_key in condition_2_keys:
        return 2
        
    if assignment_key in condition_3_keys:
        return 3
    
    # STRICT VALIDATION: Return None for unrecognized keys
    return None

def get_assignment_file_path(assignment_key):
    """
    Get the correct assignment file path based on assignment key
    Returns Path object or None if not found
    CASE-INSENSITIVE: Scans directory for matching file
    """
    base_key = get_assignment_base_key(assignment_key)
    if not base_key:
        return None
    
    assignment_dir = Path("assignment")
    if not assignment_dir.exists():
        logger.warning(f"Assignment directory does not exist: {assignment_dir}")
        return None
    
    # Case-insensitive file search
    base_key_lower = base_key.lower()
    try:
        for file in assignment_dir.iterdir():
            if file.is_file() and file.stem.lower() == base_key_lower and file.suffix == '.txt':
                logger.info(f"Found assignment file: {file}")
                return file
    except Exception as e:
        logger.error(f"Error searching for assignment file: {e}")
    
    logger.warning(f"Assignment file not found for: {base_key}")
    return None


def send_notification_email(submission_data):
    """Send notification email when a user submits their code"""
    if not os.getenv("EMAIL_ENABLED", "false").lower() == "true":
        logger.info("Email notifications disabled")
        return False
    
    try:
        # Email configuration from environment variables
        smtp_server = os.getenv("SMTP_SERVER")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        email_address = os.getenv("EMAIL_ADDRESS")
        email_password = os.getenv("EMAIL_PASSWORD")
        notification_email = os.getenv("NOTIFICATION_EMAIL")
        
        if not all([smtp_server, email_address, email_password, notification_email]):
            logger.error("Missing email configuration in environment variables")
            return False
        assert smtp_server and email_address and email_password and notification_email

        # Email body
        body = f"""New code submission received:

User ID: {submission_data['user_id']}
Assignment Key: {submission_data['assignment_key']}
Session ID: {submission_data['session_id']}
Condition: {submission_data['condition']}
Programming Language: {submission_data['programming_language']}
Submission Time: {submission_data['timestamp']}
Submission ID: {submission_data['submission_id']}

Code Length: {len(submission_data['code'])} characters
Suggestions Clicked: {submission_data['suggestion_clicked']}
Compiler Runs: {submission_data['compiler_clicked']}
Firebase Status: {submission_data['firebase_status']}

Code Preview (First 200 characters):
{submission_data['code'][:200]}{'...' if len(submission_data['code']) > 200 else ''}

---
This is an automated notification from the AI Code Assistant Research Platform.
"""
        
        msg = EmailMessage()
        msg['From'] = email_address
        msg['To'] = notification_email
        msg['Subject'] = f"New Code Submission - User {submission_data['user_id']}"
        msg.set_content(body)
        
        # Send email
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(email_address, email_password)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"Notification email sent successfully for submission {submission_data['submission_id']}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to send notification email: {str(e)}")
        logger.error(f"Email error traceback: {traceback.format_exc()}")
        return False

def detect_programming_language(code):
    """
    Detect the programming language from code content
    Returns: language name as string
    """
    if not code or not code.strip():
        return "code"
    
    code_lower = code.lower().strip()
    
    # JavaScript indicators
    if any(keyword in code_lower for keyword in [
        'console.log', 'function(', '=>', 'const ', 'let ', 'var ',
        'document.', 'window.', '$(', 'require(', 'import {', 'export '
    ]):
        return "javascript"
    
    # Python indicators
    elif any(keyword in code_lower for keyword in [
        'def ', 'import ', 'print(', 'if __name__', 'elif ', 'self.',
        'from ', 'with ', 'except:', 'finally:'
    ]):
        return "python"
    
    # Java indicators
    elif any(keyword in code_lower for keyword in [
        'public class', 'public static void', 'system.out', 'string[]',
        'public static', 'private ', 'protected ', '@override'
    ]):
        return "java"
    
    # C++ indicators
    elif any(keyword in code_lower for keyword in [
        '#include', 'std::', 'cout <<', 'cin >>', 'namespace ',
        'int main(', 'using namespace'
    ]):
        return "cpp"
    
    # C# indicators
    elif any(keyword in code_lower for keyword in [
        'using system', 'console.writeline', 'public class', 'static void main',
        'namespace ', '[system'
    ]):
        return "csharp"
    
    # HTML indicators
    elif any(keyword in code_lower for keyword in [
        '<html', '<div', '<body', '<head', '<script', '<style',
        '<!doctype'
    ]):
        return "html"
    
    # CSS indicators
    elif any(keyword in code_lower for keyword in [
        '{', '}', 'color:', 'background:', 'margin:', 'padding:',
        'font-', 'display:'
    ]) and not any(keyword in code_lower for keyword in ['function', 'var ', 'let ', 'const ']):
        return "css"
    
    # SQL indicators
    elif any(keyword in code_lower for keyword in [
        'select ', 'from ', 'where ', 'insert into', 'update ',
        'delete from', 'create table', 'alter table'
    ]):
        return "sql"
    
    # Default to generic programming language
    return "code"

async def execute_code_safely(
    code: str, language: str, timeout: int = 10, files: Optional[List[Dict[str, str]]] = None
):
    """
    Execute code safely in a subprocess with timeout and security restrictions.
    When files is provided, all files are written to a shared temp directory
    so they can import/include each other.
    """
    if not code.strip():
        return {"success": False, "output": "", "error": "No code provided"}
    
    try:
        if language == "python":
            return await execute_python_code(code, timeout, files=files)
        elif language == "c":
            return await execute_c_code(code, timeout, files=files)
        elif language == "javascript":
            return {"success": False, "output": "", "error": "JavaScript should be executed in frontend"}
        else:
            return {"success": False, "output": "", "error": f"Language '{language}' not supported for backend execution"}
    except Exception as e:
        logger.error(f"Code execution error: {e}")
        return {"success": False, "output": "", "error": f"Execution failed: {str(e)}"}

async def execute_python_code(code: str, timeout: int = 10, files: Optional[List[Dict[str, str]]] = None):
    """
    Execute Python code safely with restrictions.
    When `files` is provided, all files are written to a shared temp directory
    so they can import each other. The active file (code) is executed.
    """
    dangerous_patterns = [
        'import os', 'import sys', 'import subprocess', 'import shutil',
        'import socket', 'import urllib', 'import requests', 'import http',
        'open(', 'file(', '__import__', 'eval(', 'exec(',
        'compile(', 'globals()', 'locals()', 'vars()', 'dir(',
        'getattr(', 'setattr(', 'delattr(', 'hasattr(',
        'input(', 'raw_input('
    ]

    all_code_to_check = [code] + ([f.get("content", "") for f in files] if files else [])
    for src in all_code_to_check:
        src_lower = src.lower()
        for pattern in dangerous_patterns:
            if pattern in src_lower:
                return {
                    "success": False,
                    "output": "",
                    "error": f"Security restriction: '{pattern}' is not allowed for safety reasons"
                }

    tmpdir = tempfile.mkdtemp()
    try:
        if files and len(files) > 1:
            main_file_path = None
            for f in files:
                fname = f.get("filename", "main.py")
                if not fname.endswith(".py"):
                    fname += ".py"
                fpath = os.path.join(tmpdir, fname)
                with open(fpath, 'w', encoding='utf-8') as fh:
                    fh.write(f.get("content", ""))
                if f.get("content", "") == code:
                    main_file_path = fpath

            if not main_file_path:
                main_file_path = os.path.join(tmpdir, files[0].get("filename", "main.py"))
                if not main_file_path.endswith(".py"):
                    main_file_path += ".py"
                with open(main_file_path, 'w', encoding='utf-8') as fh:
                    fh.write(code)
        else:
            main_file_path = os.path.join(tmpdir, 'main.py')
            with open(main_file_path, 'w', encoding='utf-8') as fh:
                fh.write(code)

        process = await asyncio.create_subprocess_exec(
            sys.executable, main_file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmpdir,
            env={'PYTHONPATH': tmpdir, 'PATH': '/usr/bin:/bin'}
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            stdout_str = stdout.decode('utf-8', errors='replace')
            stderr_str = stderr.decode('utf-8', errors='replace')

            if process.returncode == 0:
                return {
                    "success": True,
                    "output": stdout_str,
                    "error": stderr_str if stderr_str else ""
                }
            else:
                return {
                    "success": False,
                    "output": stdout_str,
                    "error": stderr_str or f"Process exited with code {process.returncode}"
                }

        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "success": False,
                "output": "",
                "error": f"Code execution timed out after {timeout} seconds"
            }

    except Exception as e:
        return {
            "success": False,
            "output": "",
            "error": f"Execution error: {str(e)}"
        }
    finally:
        try:
            import shutil as _shutil
            _shutil.rmtree(tmpdir)
        except Exception:
            pass
async def execute_c_code(code: str, timeout: int = 10, files: Optional[List[Dict[str, str]]] = None):
    """
    Execute C code safely with security restrictions and async compilation.
    When `files` is provided, all files are written to a shared temp directory
    so they can #include each other. All .c files are compiled together.
    """
    dangerous_patterns = [
        '#include <unistd.h>',
        '#include <sys/',
        '#include <stdlib.h>',
        '#include <fcntl.h>',
        '#include <dirent.h>',
        '#include <pwd.h>',
        '#include <grp.h>',
        '#include <signal.h>',
        '#include <sys/socket.h>',
        '#include <netinet/',
        '#include <arpa/',
        'system(',
        'exec(',
        'fork(',
        'popen(',
        'chroot(',
        'setuid(',
        'setgid(',
        'chmod(',
        'chown(',
        'remove(',
        'unlink(',
        'rmdir(',
        'rename(',
        'tmpnam(',
        'mkstemp(',
        'fopen(',
        'freopen(',
        'fdopen(',
        'socket(',
        'bind(',
        'connect(',
    ]

    all_code_to_check = [code] + ([f.get("content", "") for f in files] if files else [])
    for src in all_code_to_check:
        src_lower = src.lower()
        for pattern in dangerous_patterns:
            if pattern.lower() in src_lower:
                return {
                    "success": False,
                    "output": "",
                    "error": f"Security restriction: '{pattern}' is not allowed for safety reasons"
                }

    tmpdir = tempfile.mkdtemp()

    try:
        c_file_paths = []
        if files and len(files) > 1:
            for f in files:
                fname = f.get("filename", "main.c")
                fpath = os.path.join(tmpdir, fname)
                with open(fpath, 'w', encoding='utf-8') as fh:
                    fh.write(f.get("content", ""))
                if fname.endswith('.c'):
                    c_file_paths.append(fpath)
        else:
            c_file_path = os.path.join(tmpdir, 'main.c')
            with open(c_file_path, 'w', encoding='utf-8') as c_file:
                c_file.write(code)
            c_file_paths = [c_file_path]

        exe_file_path = os.path.join(tmpdir, 'temp.out')

        compile_args = ['gcc'] + c_file_paths + ['-o', exe_file_path, '-std=c11', '-I', tmpdir]
        compile_process = await asyncio.create_subprocess_exec(
            *compile_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            # Wait for compilation with timeout
            compile_stdout, compile_stderr = await asyncio.wait_for(
                compile_process.communicate(), 
                timeout=10
            )
            
            compile_stderr_str = compile_stderr.decode('utf-8', errors='replace')
            
            # Check if compilation was successful
            if compile_process.returncode != 0:
                return {
                    "success": False,
                    "output": "",
                    "error": f"Compilation failed:\n{compile_stderr_str}"
                }
                
        except asyncio.TimeoutError:
            compile_process.kill()
            await compile_process.wait()
            return {
                "success": False,
                "output": "",
                "error": "Compilation timed out after 10 seconds"
            }

        # Execute the compiled binary
        exec_process = await asyncio.create_subprocess_exec(
            exe_file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={'PATH': '/usr/bin:/bin'}
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                exec_process.communicate(), 
                timeout=timeout
            )
            
            stdout_str = stdout.decode('utf-8', errors='replace')
            stderr_str = stderr.decode('utf-8', errors='replace')
            
            if exec_process.returncode == 0:
                return {
                    "success": True,
                    "output": stdout_str,
                    "error": stderr_str if stderr_str else ""
                }
            else:
                return {
                    "success": False,
                    "output": stdout_str,
                    "error": stderr_str or f"Process exited with code {exec_process.returncode}"
                }
                
        except asyncio.TimeoutError:
            exec_process.kill()
            await exec_process.wait()
            return {
                "success": False,
                "output": "",
                "error": f"Code execution timed out after {timeout} seconds"
            }
                
    except Exception as e:
        logger.error(f"C code execution error: {e}")
        return {
            "success": False,
            "output": "",
            "error": f"Execution error: {str(e)}"
        }
    finally:
        # Clean up temporary directory
        try:
            import shutil
            shutil.rmtree(tmpdir)
        except Exception as e:
            logger.warning(f"Failed to clean up temp directory {tmpdir}: {e}")
# Data Models
class AuthRequest(BaseModel):
    user_id: str
    assignment_key: str
    programming_language: str = "javascript"

class AuthResponse(BaseModel):
    status: str
    message: str
    condition: int
    problem_description: str | None = None
    session_id: str
    paste_allowed: int = 1
    tab_tracking_enabled: bool = True
    has_pre_survey: bool = False
    has_post_survey: bool = False
    programming_language: str = "javascript"
    starter_code: str | None = None
    periodic_survey_enabled: bool = False
    periodic_survey_start: int = 5
    final_survey_disabled: bool = False
    current_problem_index: int = 0
    total_problems: int = 1

class ActivityEntry(BaseModel):
    timestamp: str
    activity_type: str
    activity_details: Dict[str, Any]
    session_context: Dict[str, Any]
    
    class Config:
        # Allow arbitrary field types for complex nested data
        arbitrary_types_allowed = True

class ActivitySummary(BaseModel):
    total_keystrokes: int = 0
    total_code_executions: int = 0
    total_suggestion_clicks: int = 0
    total_history_views: int = 0
    focus_time_seconds: float = 0
    away_time_seconds: float = 0
    font_changes: int = 0
    terminal_clears: int = 0
    surveys_completed: int = 0
    peak_code_lines: int = 0
    total_ai_analyses: int = 0


class AIGuessEntry(BaseModel):
    problem_analysis: str  # FIXED: Renamed from existing_code for clarity
    next_line_suggestion: str
    full_code_suggestion: str
    timestamp: str
    analysis_type: str  # "ai_problem_guess" or "correct_description"
    user_code_snapshot: str  # NEW: Store user's code at analysis time

class SessionLog(BaseModel):
    user_id: str
    assignment_id: str
    session_id: str
    condition: int
    code_start_timestamp: str
    code_submit_timestamp: Optional[str] = None
    submitted_full_code: Optional[str] = None
    submitted_files: Optional[List[Dict[str, Any]]] = None
    # Latest server-side editor draft (within-phase durability)
    editor_draft_checkpoint: Optional[str] = None
    editor_draft_updated_at: Optional[str] = None

    # Multi-problem tracking
    current_problem_index: int = 0
    total_problems: int = 1
    quiz_id: Optional[str] = None
    per_problem_submissions: List[Dict[str, Any]] = []

    # Survey tracking
    pre_survey_status: str = "not_started"
    pre_survey_timestamp: Optional[str] = None
    pre_survey_responses: Optional[Dict[str, Any]] = None

    post_survey_status: str = "not_started"
    post_survey_timestamp: Optional[str] = None
    post_survey_responses: Optional[Dict[str, Any]] = None

    periodic_survey_count: int = 0
    periodic_surveys: List[Dict[str, Any]] = Field(default_factory=list)

    ai_guess_history: List[AIGuessEntry] = []
    correct_description_history: Optional[List[AIGuessEntry]] = None
    suggestions_only_history: Optional[List[AIGuessEntry]] = None
    problem_description: Optional[str] = None
    activity_log: List[ActivityEntry] = []
    activity_summary: ActivitySummary = ActivitySummary()


class SurveyResponse(BaseModel):
    question: str
    response: int  # 1-5 Likert scale

class SurveySubmission(BaseModel):
    user_id: str
    session_id: str
    assignment_key: str
    survey_type: str  # 'periodic' or 'final'
    responses: List[SurveyResponse]
    analysis_count: int
    timestamp: str
    ai_history_context: Optional[Dict[str, Any]] = None  # NEW: Add AI history context

class SurveyConfig(BaseModel):
    questions: List[str]
    feedback_interval: int

# Global session storage (in-memory, will be sent to Firebase on submit)
active_sessions: Dict[str, SessionLog] = {}


def get_active_quiz_session_counts(quiz_id: str) -> Dict[str, Any]:
    """Count students currently in an in-progress session for this quiz (this process only).

    Sessions are held in memory until submit; multi-worker or restarted servers only see
    their own processes' sessions.
    """
    target = str(quiz_id).strip().lower()
    by_idx: Dict[int, int] = {}
    total = 0
    for _sid, log in active_sessions.items():
        lid = getattr(log, "quiz_id", None)
        if lid is None or str(lid).strip().lower() != target:
            continue
        total += 1
        try:
            pi = int(getattr(log, "current_problem_index", 0) or 0)
        except (TypeError, ValueError):
            pi = 0
        by_idx[pi] = by_idx.get(pi, 0) + 1
    return {"total_active": total, "by_problem_index": by_idx}


def forget_active_sessions_for_quiz_user(quiz_id: str, user_id: str) -> int:
    """Drop in-memory SessionLog rows for this user on this quiz (same id scheme as Firebase)."""
    uid = str(user_id or "").strip()
    q = str(quiz_id or "").strip()
    if len(q) < 8 or not uid:
        return 0
    prefix = f"{uid}_quiz_{q[:8]}_"
    n = 0
    for sid in list(active_sessions.keys()):
        if isinstance(sid, str) and sid.startswith(prefix):
            try:
                del active_sessions[sid]
                n += 1
            except KeyError:
                pass
    return n


app = FastAPI(title=os.getenv("APP_TITLE", "AI Code Assistant API"))

# Enable CORS for frontend - MUST be before other middleware
cors_origins = os.getenv("CORS_ORIGINS", "*")
if cors_origins == "*":
    cors_origins = ["*"]
else:
    cors_origins = [origin.strip() for origin in cors_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session middleware for instructor auth (cookie-based, non-breaking for existing student flow)
from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "change-me-in-production"),
)

app.include_router(admin_router)
app.include_router(instructor_router)


def save_session_to_local(session_log: SessionLog):
    """Save session log to local file as backup"""
    try:
        logs_dir = Path(os.getenv("SUBMISSIONS_DIR", "session_logs"))
        logs_dir.mkdir(exist_ok=True)
        
        log_file = logs_dir / f"{session_log.session_id}.json"
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(session_log.dict(), f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save session log locally: {e}")


# ─── Firebase reliability: retries + incremental checkpoints ─────────────────
_firebase_checkpoint_last_at: Dict[str, float] = {}
CHECKPOINT_ACTIVITY_THROTTLE_SEC = float(os.getenv("FIREBASE_CHECKPOINT_ACTIVITY_SEC", "12"))
_last_editor_full_checkpoint_at: Dict[str, float] = {}
EDITOR_FULL_CHECKPOINT_SEC = float(os.getenv("EDITOR_FULL_CHECKPOINT_SEC", "45"))


def firebase_rtdb_set_with_retry(ref, value, *, max_attempts: int = 3) -> bool:
    """Realtime Database set() with exponential backoff."""
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            ref.set(value)
            return True
        except Exception as e:
            last_err = e
            logger.warning("Firebase set attempt %s/%s failed: %s", attempt + 1, max_attempts, e)
            if attempt < max_attempts - 1:
                time.sleep(0.35 * (2**attempt))
    logger.error("Firebase set failed after %s attempts: %s", max_attempts, last_err)
    return False


def _sanitize_firebase_key_segment(s: str) -> str:
    """RTDB keys: avoid . # $ [ ] / and long paths."""
    t = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (s or ""))
    return (t or "user")[:96]


def append_periodic_survey_to_firebase(session_id: str, survey_data: Dict[str, Any]) -> bool:
    """Append one periodic/final survey record under sessions/{id}/periodic_surveys."""
    if not firebase_db:
        return False
    try:
        session_ref = firebase_db.child("sessions").child(session_id)
        existing = session_ref.child("periodic_surveys").get() or []
        if not isinstance(existing, list):
            existing = []
        existing.append(survey_data)
        per_ref = session_ref.child("periodic_surveys")
        return firebase_rtdb_set_with_retry(per_ref, existing, max_attempts=3)
    except Exception as e:
        logger.error("append_periodic_survey_to_firebase failed: %s", e)
        return False


def load_session_log_from_disk(session_id: str) -> Optional[SessionLog]:
    """Load SessionLog JSON written by save_session_to_local."""
    try:
        logs_dir = Path(os.getenv("SUBMISSIONS_DIR", "session_logs"))
        log_file = logs_dir / f"{session_id}.json"
        if not log_file.is_file():
            return None
        raw = json.loads(log_file.read_text(encoding="utf-8"))
        return SessionLog.model_validate(raw)
    except ValidationError as e:
        logger.warning("SessionLog validation failed for %s: %s", session_id, e)
        return None
    except Exception as e:
        logger.warning("load_session_log_from_disk failed for %s: %s", session_id, e)
        return None


def find_latest_quiz_session_log_path(user_id: str, quiz_id: str) -> Optional[Path]:
    """Most recent session file for this user+quiz (disk resume when Firebase unavailable)."""
    logs_dir = Path(os.getenv("SUBMISSIONS_DIR", "session_logs"))
    if not logs_dir.is_dir():
        return None
    prefix = f"{user_id}_quiz_{quiz_id[:8]}_"
    candidates = [p for p in logs_dir.iterdir() if p.is_file() and p.suffix == ".json" and p.name.startswith(prefix)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def quiz_resume_ref(quiz_id: str, user_id: str):
    if not firebase_db:
        return None
    return firebase_db.child("quiz_resume").child(quiz_id[:48]).child(_sanitize_firebase_key_segment(user_id))


def quiz_resume_write(
    quiz_id: str,
    user_id: str,
    session_id: str,
    current_problem_index: int,
    total_problems: int,
    *,
    completed: bool = False,
) -> None:
    if not firebase_db:
        return
    try:
        ref = quiz_resume_ref(quiz_id, user_id)
        if ref is None:
            return
        payload = {
            "session_id": session_id,
            "current_problem_index": current_problem_index,
            "total_problems": total_problems,
            "completed": completed,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
        }
        firebase_rtdb_set_with_retry(ref, payload, max_attempts=3)
    except Exception as e:
        logger.warning("quiz_resume_write failed: %s", e)


def firebase_rtdb_update_with_retry(ref, value: Dict[str, Any], *, max_attempts: int = 3) -> bool:
    """Realtime Database update() with exponential backoff (shallow merge)."""
    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            ref.update(value)
            return True
        except Exception as e:
            last_err = e
            logger.warning("Firebase update attempt %s/%s failed: %s", attempt + 1, max_attempts, e)
            if attempt < max_attempts - 1:
                time.sleep(0.35 * (2**attempt))
    logger.error("Firebase update failed after %s attempts: %s", max_attempts, last_err)
    return False


def checkpoint_session_to_firebase(
    session_log: SessionLog, checkpoint_type: str, *, force: bool = False
) -> bool:
    """
    Full session snapshot under sessions/{session_id} for recovery if the user drops off
    before final submit. Throttled for high-frequency activity batches unless force=True.
    """
    if not firebase_db:
        logger.debug("Firebase checkpoint skipped (not configured)")
        return False
    sid = session_log.session_id
    now = time.time()
    if not force and checkpoint_type in ("activity_batch", "periodic_survey_batch"):
        last = _firebase_checkpoint_last_at.get(sid, 0.0)
        if now - last < CHECKPOINT_ACTIVITY_THROTTLE_SEC:
            return True  # throttled — treat as OK

    try:
        session_json = json.loads(json.dumps(session_log.dict(), default=str))
        payload = {
            "checkpoint_type": checkpoint_type,
            "checkpoint_time_utc": datetime.now(timezone.utc).isoformat(),
            "user_id": session_log.user_id,
            "assignment_id": session_log.assignment_id,
            "condition": session_log.condition,
            "session": session_json,
        }
        # Under .../checkpoint so we do not overwrite sessions/{id}/periodic_surveys etc.
        ref = firebase_db.child("sessions").child(sid).child("checkpoint")
        ok = firebase_rtdb_set_with_retry(ref, payload, max_attempts=3)
        if ok and checkpoint_type in ("activity_batch", "periodic_survey_batch"):
            _firebase_checkpoint_last_at[sid] = time.time()
        elif ok:
            _firebase_checkpoint_last_at[sid] = time.time()
        return ok
    except Exception as e:
        logger.error("checkpoint_session_to_firebase failed: %s", e)
        return False


def checkpoint_pre_post_survey_file_payload_to_firebase(
    session_id: str,
    user_id: str,
    assignment_key: str,
    survey_type: str,
    file_data: Dict[str, Any],
) -> bool:
    """If SessionLog is not in memory, still persist submitted survey JSON to Firebase."""
    if not firebase_db:
        return False
    try:
        payload = {
            "checkpoint_type": f"survey_{survey_type}_orphan",
            "checkpoint_time_utc": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "assignment_id": assignment_key,
            "survey_type": survey_type,
            "survey_file_payload": json.loads(json.dumps(file_data, default=str)),
        }
        ref = firebase_db.child("sessions").child(session_id).child("orphan_survey")
        return firebase_rtdb_set_with_retry(ref, payload, max_attempts=3)
    except Exception as e:
        logger.error("checkpoint_pre_post_survey_file_payload_to_firebase failed: %s", e)
        return False

def extract_survey_ai_history_summary(
    session_log: SessionLog, survey_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Extract a summary of AI history context for surveys"""
    if not survey_data:
        return {
            'total_surveys_completed': 0,
            'ai_analyses_per_survey': []
        }
    
    periodic_surveys = survey_data.get('periodic_surveys', []) if isinstance(survey_data.get('periodic_surveys'), list) else []
    final_survey = survey_data.get('final_survey')
    
    summary = {
        'total_surveys_completed': len(periodic_surveys) + (1 if final_survey else 0),
        'ai_analyses_per_survey': []
    }
    
    # Track AI analyses at each periodic survey point
    for survey in periodic_surveys:
        summary['ai_analyses_per_survey'].append({
            'survey_timestamp': survey.get('timestamp'),
            'analyses_count_at_survey': survey.get('analysisCount', 0),
            'ai_history_captured': True
        })
    
    # Add final survey context if it exists
    if final_survey:
        summary['final_survey_ai_context'] = {
            'timestamp': final_survey.get('timestamp'),
            'total_analyses_at_completion': session_log.activity_summary.total_ai_analyses,
            'ai_history_captured': True
        }
    
    return summary


# OpenAI Configuration
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("OPENAI_API_KEY not found in environment variables")

client = OpenAI(api_key=API_KEY)

# Sidebar hidden prefixes
SIDE_PANEL_HIDDEN = os.getenv("SIDE_PANEL_HIDDEN", "").lower().split(",")
SIDE_PANEL_HIDDEN = [prefix.strip() for prefix in SIDE_PANEL_HIDDEN if prefix.strip()]

def get_response(prompt):
    """Get response from OpenAI GPT model with error handling"""
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "system", "content": prompt}],
            model="gpt-4.1-nano",
        )
        response = chat_completion.choices[0].message.content
        return response.strip() if response else ""
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return f"Error getting AI response: {str(e)}"

def _clamp_problem_description_to_lines(text: str, max_lines: int = 3) -> str:
    """Keep explanation short: at most `max_lines` non-empty lines (LeetCode-style brevity)."""
    if not text or not str(text).strip():
        return text
    lines = []
    for ln in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = ln.strip()
        if s:
            lines.append(s)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)


_LEADING_GUESS = re.compile(
    r"^\s*(?:this\s+code\s+is\s+trying\s+(?:to\s+)?|you\s+are\s+trying\s+(?:to\s+)?)",
    re.IGNORECASE,
)


def _ensure_problem_guess_framing(text: str) -> str:
    """
    Make it obvious the AI is guessing the programmer's intent: first line reads
    'You are trying to …' (second person; legacy 'This code is trying to' is normalized away).
    """
    if not text or not str(text).strip():
        return text
    t0 = str(text).strip()
    if t0.lower().startswith("error getting") or t0.lower().startswith("no code provided"):
        return text
    raw_lines = [ln.strip() for ln in str(text).replace("\r\n", "\n").split("\n") if ln.strip()]
    if not raw_lines:
        return text
    first = raw_lines[0]
    remainder = _LEADING_GUESS.sub("", first).strip()
    if not remainder:
        remainder = "infer what programming task you are solving from the code so far."
    elif remainder[0].isupper():
        remainder = remainder[0].lower() + remainder[1:]
    raw_lines[0] = "You are trying to " + remainder
    return "\n".join(raw_lines)


def analyze_problem_statement(existing_code, language=None):
    """Infer a concise LeetCode-style problem statement from the current code."""
    if not existing_code or not existing_code.strip():
        return "No code provided for analysis"

    if not language:
        language = detect_programming_language(existing_code)

    what_problem_is_this = f"""
    You are guessing what problem the programmer is trying to solve from this incomplete {language} code.

    ```{language}
    {existing_code}
    ```

    Write a short description so the reader knows this is the AI's guess about their goal (not a fact about the assignment).

    Format:
    - Line 1 MUST begin with these exact words: You are trying to
      (then continue on the same line with a short phrase: what you seem to be solving or building.)
    - Optional lines 2–3: extra LeetCode-style detail (goal, constraint, or edge case) in plain English.
    - No markdown, bullets, "Example:", code fences, or solution steps.

    Limits: at most 3 lines total, ~280 characters total.
    """

    problem_description = get_response(what_problem_is_this)
    problem_description = _ensure_problem_guess_framing(problem_description)
    return _clamp_problem_description_to_lines(problem_description, max_lines=3)


def _normalized_code_lines(code: str) -> list:
    return code.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _line_is_comment_only(line: str, language: Optional[str]) -> bool:
    """True if the line is only a comment (used to sanitize short AI snippets)."""
    s = line.strip()
    if not s:
        return False
    lang = (language or "").lower()
    if lang == "python":
        return s.startswith("#")
    if lang in ("html", "xml"):
        return s.startswith("<!--")
    if lang == "css":
        return s.startswith("/*") or s.startswith("//")
    if lang in (
        "javascript",
        "typescript",
        "js",
        "ts",
        "java",
        "cpp",
        "csharp",
        "c",
        "go",
        "rust",
        "swift",
        "kotlin",
        "scala",
    ):
        return s.startswith("//") or (s.startswith("/*") and "*/" in s)
    if s.startswith("#") or s.startswith("//"):
        return True
    if s.startswith("/*") and s.endswith("*/"):
        return True
    return False


def _strip_comment_only_lines(text: str, language: Optional[str]) -> str:
    """Drop comment-only lines from 1–2 line suggestions if the model ignores instructions."""
    if not text or not text.strip():
        return text
    lines = text.split("\n")
    kept = [ln for ln in lines if not _line_is_comment_only(ln, language)]
    out = "\n".join(kept).strip()
    if out:
        return out
    # Entire suggestion was comment-only — discard so callers can fall back
    return ""


def _runtime_blocked_substrings_for_suggestions(language: Optional[str]) -> tuple:
    """
    Substrings that must not appear in AI suggestions — align with execute_python_code /
    sandbox limits (no stdin, no unsafe imports, etc.).
    """
    lang = (language or "").lower()
    if lang == "python":
        return (
            "input(",
            "raw_input(",
            "open(",
            "file(",
            "eval(",
            "exec(",
            "__import__(",
            "import os",
            "import sys",
            "import subprocess",
            "import shutil",
            "import socket",
            "import urllib",
            "import requests",
            "import http",
        )
    if lang in ("c", "cpp"):
        return ("scanf(", "gets(", "getchar(", "fgets(stdin", "sscanf(")
    if lang in ("javascript", "typescript", "js", "ts"):
        return ("prompt(",)
    if lang == "java":
        return ("system.in", "scanner(system.in)", "new scanner(system.in)")
    return tuple()


def _contains_blocked_suggestion(text: str, language: Optional[str]) -> bool:
    if not text or not str(text).strip():
        return False
    tl = text.lower()
    for sub in _runtime_blocked_substrings_for_suggestions(language):
        if sub in tl:
            return True
    return False


def _strip_blocked_suggestion_lines(text: str, language: Optional[str]) -> str:
    """Drop lines that contain blocked substrings; return empty if nothing remains."""
    if not text or not str(text).strip():
        return text
    lines = str(text).replace("\r\n", "\n").split("\n")
    kept = [ln for ln in lines if not _contains_blocked_suggestion(ln, language)]
    return "\n".join(kept).strip()


def _suggestion_runtime_prompt_rule(language: Optional[str]) -> str:
    """Short rule text injected into AI prompts (per language)."""
    lang = (language or "").lower()
    if lang == "python":
        return (
            "- The runner cannot use interactive stdin or blocked APIs: NEVER suggest "
            "input(, raw_input(, open(, file(, eval(, exec(, __import__(, or imports of "
            "os, sys, subprocess, shutil, socket, urllib, requests, http. Use literals, "
            "parameters, or existing variables instead."
        )
    if lang in ("c", "cpp"):
        return (
            "- The runner has no interactive stdin: NEVER suggest scanf(, gets(, getchar(, "
            "sscanf( reading from stdin, or fgets(..., stdin). Use fixed values or function arguments."
        )
    if lang in ("javascript", "typescript", "js", "ts"):
        return (
            "- Do not suggest prompt( or other blocking stdin-style browser dialogs for input; "
            "use literals or variables already in the code."
        )
    if lang == "java":
        return (
            "- The environment may not support interactive stdin: avoid System.in, Scanner(System.in), "
            "and console readLine for input; use literals or method parameters."
        )
    return (
        "- Do not suggest code that requires interactive stdin or sandbox-blocked APIs for this IDE."
    )


def _next_line_from_full_completion(
    existing_code: str,
    full_code: str,
    max_lines: int = 2,
    language: Optional[str] = None,
) -> str:
    """
    When the next-line model returns empty, derive 1–2 new lines from the concurrent
    full-code completion if it extends the user's current buffer.
    """
    if not full_code or not str(full_code).strip():
        return ""
    lang = language or detect_programming_language(existing_code)
    ec = existing_code.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    fc = full_code.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not fc:
        return ""
    if ec and fc.startswith(ec):
        suffix = fc[len(ec) :].lstrip("\n")
        lines = [ln for ln in suffix.split("\n") if ln.strip()]
        if not lines:
            return ""
        out = "\n".join(lines[:max_lines]).strip()
        return _strip_blocked_suggestion_lines(out, lang)
    ec_lines = ec.split("\n") if ec else []
    fc_lines = fc.split("\n")
    if ec_lines and len(fc_lines) > len(ec_lines) and fc_lines[: len(ec_lines)] == ec_lines:
        rest = fc_lines[len(ec_lines) :]
        rest = [ln for ln in rest if ln.strip()]
        if rest:
            out = "\n".join(rest[:max_lines]).strip()
            return _strip_blocked_suggestion_lines(out, lang)
    return ""


# Keywords for "cursor at end of line but word is incomplete" (e.g. retu → return)
_PYTHON_KW = frozenset(keyword.kwlist) | {"True", "False", "None"}
_JS_KW = frozenset(
    """
    break case catch class const continue debugger default delete do else export extends
    finally for function if import in instanceof let new return super switch this throw
    try typeof var void while with yield await async null undefined true false
    """.split()
)


def _last_identifier_at_line_end(stripped: str) -> str:
    """Last identifier token on the line (handles `x = retu` → retu)."""
    m = re.search(r"([A-Za-z_][\w]*)$", stripped.rstrip())
    return m.group(1) if m else ""


def _is_strict_prefix_of_any_keyword(word: str, keywords: frozenset) -> bool:
    """True if `word` is not a full keyword but is a strict prefix of some keyword (retu → return)."""
    if len(word) < 2:
        return False
    if word in keywords:
        return False
    return any(len(kw) > len(word) and kw.startswith(word) for kw in keywords)


def _line_looks_incomplete_python(stripped: str) -> bool:
    """Heuristic: line at cursor end still needs characters on the same line."""
    if not stripped or stripped.endswith("#"):
        return False
    stack: list = []
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if stack:
                stack.pop()
    if stack:
        return True
    if stripped.endswith((",", "\\")):
        return True
    if re.search(r"[=+\-*/%]$", stripped) and not stripped.endswith("=="):
        return True
    if re.search(r"\b(and|or|not|is|in)$", stripped):
        return True
    if stripped.count('"') % 2 == 1 or stripped.count("'") % 2 == 1:
        return True
    if re.search(r"\b(def|class)\s+\w+$", stripped):
        return True
    if stripped.endswith("("):
        return True
    return False


def _line_looks_incomplete_generic(stripped: str) -> bool:
    if not stripped:
        return False
    stack: list = []
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if stack:
                stack.pop()
    if stack:
        return True
    if stripped.endswith((",", "(", "\\")):
        return True
    if stripped.count('"') % 2 == 1 or stripped.count("'") % 2 == 1:
        return True
    return False


def infer_suggestion_mode(code: str, line_number: int, column: int, language: Optional[str]) -> str:
    """
    complete_current: insert at cursor (finish current line).
    next_line: insert as new line after current position (new statement).
    """
    lines = _normalized_code_lines(code)
    if not lines:
        return "next_line"
    line_number = max(1, min(line_number, len(lines)))
    line = lines[line_number - 1]
    col = max(1, column) - 1

    if len(line) > 0 and col < len(line):
        return "complete_current"

    stripped = line.rstrip()
    if not stripped:
        return "next_line"

    lang = (language or "").lower()
    # Cursor at end of line: still "mid-word" if typing a keyword prefix (e.g. retu → return)
    last_id = _last_identifier_at_line_end(stripped)
    if last_id:
        if lang == "python":
            if _is_strict_prefix_of_any_keyword(last_id, _PYTHON_KW):
                return "complete_current"
        elif lang in ("javascript", "typescript", "js", "ts"):
            if _is_strict_prefix_of_any_keyword(last_id, _JS_KW):
                return "complete_current"

    if lang == "python":
        if _line_looks_incomplete_python(stripped):
            return "complete_current"
    else:
        if _line_looks_incomplete_generic(stripped):
            return "complete_current"
    return "next_line"


def predict_complete_line_suffix(
    problem_description: str,
    existing_code: str,
    line_number: int,
    column: int,
    language: Optional[str] = None,
) -> str:
    """Return text to insert at cursor to continue the current line (suffix)."""
    if not existing_code or not existing_code.strip():
        return ""

    if not language:
        language = detect_programming_language(existing_code)

    lines = _normalized_code_lines(existing_code)
    if line_number < 1 or line_number > len(lines):
        line_number = len(lines)
    line = lines[line_number - 1]
    col = max(1, column) - 1
    col = min(col, len(line))
    before = line[:col]
    after = line[col:]

    prompt = f"""
You are an expert {language} programmer.

Problem / code context summary:
{problem_description}

Full current code:
```{language}
{existing_code}
```

The cursor is on line {line_number}. Text on that line BEFORE the cursor (do not repeat this): {before!r}
Text AFTER the cursor on the same line: {after!r}

Output ONLY the characters to INSERT at the cursor position to properly complete or continue THIS LINE.
Rules:
- Do NOT repeat the "before cursor" text.
- Do NOT include a leading newline at the start.
- At most 2 lines total if a line break is strictly required; otherwise one line.
- No explanations, markdown fences, or commentary — only the insertable characters.
- Do NOT output comments of any kind (no #, //, /* */, <!-- -->, docstrings, or inline comments).
{_suggestion_runtime_prompt_rule(language)}
"""
    suffix = get_response(prompt.strip())
    suffix = re.sub(r"```\w*\n?", "", suffix)
    suffix = re.sub(r"```\n?", "", suffix)
    suffix = suffix.strip()
    suffix = _strip_comment_only_lines(suffix, language)

    # Prefix-overlap safety: if the AI repeated some or all of the
    # "before cursor" text, strip the overlapping prefix so we don't
    # produce e.g. "retreturn" when "before" is "ret" and suffix is "return".
    if before and suffix:
        # Work on content only (no indentation) to avoid false matches
        before_content = before.lstrip().lower()
        # Strip leading whitespace from suffix — it's inserted at cursor,
        # so AI-supplied indent is wrong / duplicate.
        suffix = suffix.lstrip()
        suffix_rest = ""
        if "\n" in suffix:
            first_nl = suffix.index("\n")
            suffix_rest = suffix[first_nl:]
            suffix_first = suffix[:first_nl]
        else:
            suffix_first = suffix

        if before_content and suffix_first:
            sf_lower = suffix_first.lower()
            if sf_lower.startswith(before_content):
                # Full echo: AI repeated the typed text at the start
                suffix_first = suffix_first[len(before_content):]
            else:
                # Partial overlap: longest tail of typed text matching
                # head of suffix (e.g. "ret" overlaps "return")
                for overlap_len in range(min(len(before_content), len(sf_lower)), 0, -1):
                    if before_content.endswith(sf_lower[:overlap_len]):
                        suffix_first = suffix_first[overlap_len:]
                        break

        suffix = (suffix_first + suffix_rest).rstrip()

    lines_out = suffix.split("\n")
    if len(lines_out) > 2:
        suffix = "\n".join(lines_out[:2])
    if _contains_blocked_suggestion(suffix, language):
        return ""
    return suffix if suffix else ""


def predict_next_line(problem_description, existing_code, language=None):
    """Predict the next line of code based on problem and existing code - language agnostic"""

    if not existing_code or not existing_code.strip():
        return ""

    if not language:
        language = detect_programming_language(existing_code)
    
    # Language-specific syntax examples
    syntax_examples = {
        "javascript": [
            "    } else {",
            "        return result;",
            "    console.log(message);",
            "function calculate() {"
        ],
        "python": [
            "    else:",
            "        return n * factorial(n - 1)",
            "    pass",
            "def another_function():"
        ],
        "java": [
            "    } else {",
            "        return result;",
            "    System.out.println(message);",
            "public void method() {"
        ],
        "html": [
            "    </div>",
            "    <p>Content here</p>",
            "</body>",
            "<div class=\"container\">"
        ]
    }
    
    examples = syntax_examples.get(language, [
        "    }",
        "    doSomething();",
        "    variable = value;",
        "function_name() {"
    ])
    
    # Show the AI the last few lines explicitly so it can't miss them
    code_lines = _normalized_code_lines(existing_code)
    last_lines = code_lines[-5:] if len(code_lines) > 5 else code_lines
    last_lines_str = "\n".join(f"  {ln}" for ln in last_lines)

    what_is_the_next_line = f"""
    You are an expert programmer. Based on this problem: {problem_description}
    
    Current {language} code:
    ```{language}
    {existing_code}
    ```
    
    The last lines already written are:
    {last_lines_str}
    
    The cursor is at the END of the code above, on a new blank line.
    Generate ONLY the next single line of code that should logically follow AFTER the last line above.
    
    Rules:
    - Provide EXACTLY ONE line of code (maximum 2 lines if absolutely necessary)
    - Maintain proper {language} syntax and indentation
    - Output executable code only: no explanations, no markdown, no multiple code blocks
    - Do NOT include any comments: no #, //, /* */, <!-- -->, docstrings, or inline comments
    - CRITICAL: NEVER repeat or duplicate any line that already exists in the code — especially function definitions, return statements, or print calls that are already present
    - The suggestion must CONTINUE the program, not rewrite what exists
    - If a function body is complete (has a return), suggest calling it or defining the NEXT function
    - Focus on what is MISSING, not what is already written
    {_suggestion_runtime_prompt_rule(language)}
    
    Examples of good responses for {language}:
    {chr(10).join([f'    - "{example}"' for example in examples[:4]])}    
    Next line:
    """
    
    next_line = get_response(what_is_the_next_line)
    next_line = _strip_comment_only_lines(next_line, language)

    # Post-processing: reject if the suggestion duplicates an existing line
    if next_line:
        existing_stripped = {ln.strip().lower() for ln in code_lines if ln.strip()}
        suggestion_lines = next_line.strip().split("\n")
        kept = [ln for ln in suggestion_lines if ln.strip().lower() not in existing_stripped]
        next_line = "\n".join(kept).strip()
        # If dedup removed every line, keep the model's first line (better than empty)
        if not next_line and suggestion_lines:
            for ln in suggestion_lines:
                if ln.strip():
                    next_line = ln.strip()
                    break

    if _contains_blocked_suggestion(next_line, language):
        return ""
    return next_line

def generate_full_code_suggestion(problem_description, existing_code, is_correct_description=False, language=None):
    """Generate full code completion suggestion"""
    if not existing_code or not existing_code.strip():
        return ""

    if not language:
        language = detect_programming_language(existing_code)
    
    description_type = "correct problem description" if is_correct_description else "AI analysis"
    
    prompt = f"""
    You are an expert {language} programmer. Based on this {description_type}: {problem_description}
    
    Current {language} code:
    ```{language}
    {existing_code}
    ```
    
    Generate a complete, working solution that builds upon the existing code. 
    
    Rules:
    - Complete the existing code structure
    - Don't change anything of the existing code. 
    - Don't provide any explanation of the code.
    - Use proper {language} syntax.
    - Do NOT add comments of any kind: no line comments (#, //), block comments (/* */), HTML comments, or docstrings
    - Don't include extraneous text outside the code.
    - Output should be in markdown text.
    {_suggestion_runtime_prompt_rule(language)}

    Complete code:
    """
    
    full_suggestion = get_response(prompt)
    
    # Clean the response
    full_suggestion = re.sub(r'```\w*\n?', '', full_suggestion)
    full_suggestion = re.sub(r'```\n?', '', full_suggestion)
    full_suggestion = full_suggestion.strip()
    # Drop whole lines that are only comments (best-effort; avoids stripping # inside strings)
    full_lines = full_suggestion.split("\n")
    full_suggestion = "\n".join(
        ln for ln in full_lines if not _line_is_comment_only(ln, language)
    ).strip()

    if _contains_blocked_suggestion(full_suggestion, language):
        return ""

    return full_suggestion


async def get_ai_next_line_async(problem_description, code, language=None):
    """Async wrapper for AI next line prediction"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, predict_next_line, problem_description, code, language)

async def get_correct_next_line_async(correct_description, code, language=None):
    """Async wrapper for correct description next line prediction"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, predict_next_line, correct_description, code, language)


async def get_complete_line_suffix_async(problem_description, code, line_number, column, language=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, predict_complete_line_suffix, problem_description, code, line_number, column, language
    )


async def get_correct_complete_line_suffix_async(correct_description, code, line_number, column, language=None):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, predict_complete_line_suffix, correct_description, code, line_number, column, language
    )


async def get_full_code_suggestion_async(problem_description, code, is_correct_description=False, language=None):
    """Async wrapper for full code suggestion"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, generate_full_code_suggestion, problem_description, code, is_correct_description, language)

class CodeRequest(BaseModel):
    code: str
    user_id: Optional[str] = None
    assignment_key: Optional[str] = None
    session_id: Optional[str] = None
    programming_language: Optional[str] = None
    cursor_line: Optional[int] = None
    cursor_column: Optional[int] = None

class CodeResponse(BaseModel):
    analysis: str
    next_line: str
    correct_next_line: Optional[str] = None
    suggestion_kind: str = "next_line"  # "complete_current" | "next_line"


class FileItem(BaseModel):
    filename: str
    content: str

class ExecuteRequest(BaseModel):
    code: str
    language: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    files: Optional[List[FileItem]] = None

class ExecuteResponse(BaseModel):
    success: bool
    output: str
    error: str
    execution_time: float


@app.post("/analyze", response_model=CodeResponse)
async def analyze_code(request: CodeRequest):
    """
    Continuously analyze the provided code using OpenAI and return analysis + next line suggestion
    FIXED: Generate separate suggestions for AI guess and correct description
    """
    try:
        code = request.code.strip()
        session_id = request.session_id
        
        max_code_length = int(os.getenv("MAX_CODE_LENGTH", "10000"))
        if len(code) > max_code_length:
            raise HTTPException(status_code=400, detail=f"Code too long - please keep under {max_code_length} characters")
        
        if not code:
            return CodeResponse(
                analysis="Start typing code to get AI-powered problem analysis...",
                next_line="",
                suggestion_kind="next_line",
            )
        
        language = request.programming_language or detect_programming_language(code)

        # Skip heavy suggestions only for tiny snippets (was 10 — too strict, blocked early edits)
        min_chars = int(os.getenv("MIN_CODE_CHARS_FOR_SUGGESTION", "3"))
        if len(code.strip()) < min_chars:
            return CodeResponse(
                analysis=f"Code snippet in progress: {code.strip()} (detected: {language})",
                next_line="",
                suggestion_kind="next_line",
            )

        # Step 1: Analyze what problem the code is trying to solve (thread pool — do not block event loop)
        loop = asyncio.get_event_loop()
        problem_description = await loop.run_in_executor(
            None, lambda: analyze_problem_statement(code, language=language)
        )
        
        # Clean problem description
        problem_description = re.sub(r'^\*\*.*?\*\*:\s*', '', problem_description)
        problem_description = re.sub(r'^Problem.*?:\s*', '', problem_description, flags=re.IGNORECASE)
        problem_description = problem_description.strip()
        problem_description = _clamp_problem_description_to_lines(problem_description, max_lines=3)

        # Always use next_line mode (suggestions only on new lines)
        suggestion_kind = "next_line"
        
        # Step 2 & 3: Concurrent API calls for better performance
        session_log = None
        if session_id and session_id in active_sessions:
            session_log = active_sessions[session_id]
        
        # Prepare concurrent tasks
        tasks = []
        
        ai_next_line_task = get_ai_next_line_async(problem_description, code, language=language)
        ai_full_code_task = get_full_code_suggestion_async(problem_description, code, is_correct_description=False, language=language)
        tasks.extend([ai_next_line_task, ai_full_code_task])

        # For condition 1, also get correct description suggestions concurrently
        correct_next_line_task = None
        correct_full_code_task = None
        if session_log and session_log.condition == 1 and session_log.problem_description:
            correct_next_line_task = get_correct_next_line_async(session_log.problem_description, code, language=language)
            correct_full_code_task = get_full_code_suggestion_async(session_log.problem_description, code, is_correct_description=True, language=language)
            tasks.extend([correct_next_line_task, correct_full_code_task])
        
        # Execute all tasks concurrently
        if session_log and session_log.condition == 1 and session_log.problem_description:
            # Condition 1: 4 concurrent calls
            ai_next_line, ai_full_code_suggestion, correct_next_line, correct_full_code_suggestion = await asyncio.gather(*tasks)
        else:
            # Conditions 2 & 3: 2 concurrent calls
            ai_next_line, ai_full_code_suggestion = await asyncio.gather(*tasks)
            correct_next_line = ai_next_line  # Default fallback
            correct_full_code_suggestion = ai_full_code_suggestion
        
        # Clean AI next line suggestion
        ai_next_line = re.sub(r'```\w*\n?', '', ai_next_line)
        ai_next_line = re.sub(r'```\n?', '', ai_next_line)
        ai_next_line = re.sub(r'^Next line[s]?:\s*', '', ai_next_line, flags=re.IGNORECASE)
        ai_next_line = re.sub(r'^Here[\'s\s]+.*?:\s*', '', ai_next_line, flags=re.IGNORECASE)
        
        # Ensure only 1-2 lines maximum
        lines = ai_next_line.strip().split('\n')
        if len(lines) > 2:
            ai_next_line = '\n'.join(lines[:2])
        else:
            ai_next_line = ai_next_line.strip()
        
        # Ensure we have something useful (suffix can be a single character)
        if not ai_next_line or not str(ai_next_line).strip():
            ai_next_line = ""

        # Fallback: next-line model often returns empty after dedup/comments; full-code task may still succeed
        if not ai_next_line.strip() and ai_full_code_suggestion:
            fb = _next_line_from_full_completion(
                code, ai_full_code_suggestion, max_lines=2, language=language
            )
            if fb:
                ai_next_line = fb

        # Clean correct next line suggestion (for condition 1)
        if session_log and session_log.condition == 1 and correct_next_line != ai_next_line:
            correct_next_line = re.sub(r'```\w*\n?', '', correct_next_line)
            correct_next_line = re.sub(r'```\n?', '', correct_next_line)
            correct_next_line = re.sub(r'^Next line[s]?:\s*', '', correct_next_line, flags=re.IGNORECASE)
            correct_next_line = re.sub(r'^Here[\'s\s]+.*?:\s*', '', correct_next_line, flags=re.IGNORECASE)
            
            # Ensure only 1-2 lines maximum
            correct_lines = correct_next_line.strip().split('\n')
            if len(correct_lines) > 2:
                correct_next_line = '\n'.join(correct_lines[:2])
            else:
                correct_next_line = correct_next_line.strip()
            
            if not correct_next_line or not str(correct_next_line).strip():
                correct_next_line = ai_next_line

        if (
            session_log
            and session_log.condition == 1
            and getattr(session_log, "problem_description", None)
            and (not correct_next_line or not str(correct_next_line).strip())
            and correct_full_code_suggestion
        ):
            cfb = _next_line_from_full_completion(
                code, correct_full_code_suggestion, max_lines=2, language=language
            )
            if cfb:
                correct_next_line = cfb

        # Log this analysis to session if session_id provided
        if session_log:
            # Create AI guess entry for problem guessing
            ai_guess_entry = AIGuessEntry(
                problem_analysis=problem_description,
                next_line_suggestion=ai_next_line,
                full_code_suggestion=ai_full_code_suggestion,
                timestamp=datetime.now().isoformat(),
                analysis_type="ai_problem_guess",
                user_code_snapshot=code
            )
            session_log.ai_guess_history.append(ai_guess_entry)
            
# For condition 1, also add to correct description history
            if session_log.condition == 1:
                if session_log.correct_description_history is None:
                    session_log.correct_description_history = []
                
                correct_problem_description = session_log.problem_description
                if not correct_problem_description:
                    logger.warning(f"No problem description available for session {session_id}")
                    correct_problem_description = problem_description  # Fallback to AI analysis
                
                # Use the already generated correct full code suggestion
                # (generated concurrently above)
                
                # Generate suggestion based on correct problem description
                correct_desc_entry = AIGuessEntry(
                    problem_analysis=correct_problem_description,
                    next_line_suggestion=correct_next_line,  # FIXED: Use correct next line
                    full_code_suggestion=correct_full_code_suggestion,
                    timestamp=datetime.now().isoformat(),
                    analysis_type="correct_description",
                    user_code_snapshot=code
                )
                session_log.correct_description_history.append(correct_desc_entry)
            
            # For condition 3, also add to suggestions-only history
            elif session_log.condition == 3:
                if session_log.suggestions_only_history is None:
                    session_log.suggestions_only_history = []
                
                # For condition 3, create a simplified entry with just the suggestion
                suggestions_entry = AIGuessEntry(
                    problem_analysis="Suggestions only mode",  # Placeholder text
                    next_line_suggestion=ai_next_line,
                    full_code_suggestion=ai_full_code_suggestion,
                    timestamp=datetime.now().isoformat(),
                    analysis_type="suggestions_only",
                    user_code_snapshot=code
                )
                session_log.suggestions_only_history.append(suggestions_entry)
            
            # Update local backup only
            save_session_to_local(session_log)
        
        # FIXED: Return both suggestions
        return CodeResponse(
            analysis=problem_description,
            next_line=ai_next_line,
            correct_next_line=correct_next_line if session_log and session_log.condition == 1 else None,
            suggestion_kind=suggestion_kind,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in analysis: {str(e)}")
        error_msg = str(e).lower()
        
        if "rate limit" in error_msg or "quota" in error_msg:
            fallback_analysis = "Rate Limit: API temporarily unavailable"
        elif "network" in error_msg or "connection" in error_msg:
            fallback_analysis = "Connection Issue: Network error"
        else:
            fallback_analysis = f"Analysis Error: {str(e)[:100]}"
        
        return CodeResponse(
            analysis=fallback_analysis,
            next_line="",
            suggestion_kind="next_line",
        )
    

@app.post("/execute", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest):
    """
    Execute code safely on the backend
    """
    try:
        code = request.code.strip()
        language = request.language.lower()
        
        if not code:
            return ExecuteResponse(
                success=False,
                output="",
                error="No code provided for execution",
                execution_time=0.0
            )
        
        max_code_length = int(os.getenv("MAX_CODE_LENGTH", "10000"))
        if len(code) > max_code_length:
            return ExecuteResponse(
                success=False,
                output="",
                error=f"Code too long - please keep under {max_code_length} characters",
                execution_time=0.0
            )
        
        files_dicts = None
        if request.files:
            total_len = sum(len(f.content) for f in request.files)
            if total_len > max_code_length * 5:
                return ExecuteResponse(
                    success=False,
                    output="",
                    error=f"Total code across all files is too long",
                    execution_time=0.0
                )
            files_dicts = [{"filename": f.filename, "content": f.content} for f in request.files]

        start_time = time.time()
        result = await execute_code_safely(code, language, timeout=10, files=files_dicts)
        execution_time = time.time() - start_time
        
        return ExecuteResponse(
            success=result["success"],
            output=result["output"],
            error=result["error"],
            execution_time=execution_time
        )
        
    except Exception as e:
        logger.error(f"Execute endpoint error: {str(e)}")
        return ExecuteResponse(
            success=False,
            output="",
            error=f"Server error: {str(e)}",
            execution_time=0.0
        )

class SubmitRequest(BaseModel):
    code: str
    user_id: Optional[str] = None
    assignment_key: Optional[str] = None
    session_id: Optional[str] = None
    programming_language: Optional[str] = None
    timestamp: Optional[str] = None
    suggestion_clicked: int = 0
    compiler_clicked: int = 0
    survey_data: Optional[Dict[str, Any]] = None
    files: Optional[List[FileItem]] = None
    # Full client activity log (may be ahead of server /log-activities batches)
    activity_logs: Optional[List[Dict[str, Any]]] = None


def _merge_client_activity_logs(session_log: SessionLog, raw_logs: Optional[List[Dict[str, Any]]]) -> None:
    """Union server and client activity rows so Firebase keeps accept/reject and per-problem context."""
    if not raw_logs or not isinstance(raw_logs, list):
        return
    parsed: List[ActivityEntry] = []
    for raw in raw_logs:
        if not isinstance(raw, dict):
            continue
        try:
            parsed.append(ActivityEntry(**raw))
        except Exception as ex:
            logger.debug("Skipping malformed activity entry: %s", ex)
            continue
    if not parsed:
        return
    merged: Dict[tuple, ActivityEntry] = {}
    for e in session_log.activity_log:
        merged[(e.timestamp, e.activity_type)] = e
    for e in parsed:
        merged[(e.timestamp, e.activity_type)] = e
    session_log.activity_log = sorted(merged.values(), key=lambda x: x.timestamp)

class SubmitResponse(BaseModel):
    status: str
    message: str
    submission_id: str
    timestamp: str
    firebase_status: str
    logout_required: bool = True

@app.post("/submit", response_model=SubmitResponse)
async def submit_code(request: SubmitRequest):
    """
    Submit final code and upload all session data to Firebase
    AUTOMATICALLY LOGS OUT USER after successful submission
    """
    try:
        code = request.code.strip()
        session_id = request.session_id
        user_id = request.user_id or "anonymous"
        assignment_key = request.assignment_key or "unknown"
        programming_language = request.programming_language or "unknown"
        timestamp = request.timestamp or datetime.now().isoformat()
        suggestion_clicked = request.suggestion_clicked
        compiler_clicked = request.compiler_clicked
        
        if not code:
            raise HTTPException(status_code=400, detail="No code provided for submission")
        
        max_submission_length = int(os.getenv("MAX_SUBMISSION_LENGTH", "15000"))
        if len(code) > max_submission_length:
            raise HTTPException(status_code=400, detail=f"Code too long - please keep under {max_submission_length} characters")
        
        submitted_files = None
        if request.files:
            submitted_files = [{"filename": f.filename, "content": f.content} for f in request.files]

        # Get session log
        session_log = None
        if session_id and session_id in active_sessions:
            session_log = active_sessions[session_id]
            session_log.code_submit_timestamp = timestamp
            session_log.submitted_full_code = code
            if submitted_files:
                session_log.submitted_files = submitted_files
            # Record final problem code for instructor analytics (next-problem only stores intermediate)
            if session_log.quiz_id and session_log.total_problems >= 1:
                session_log.per_problem_submissions.append({
                    "problem_index": session_log.current_problem_index,
                    "code": code,
                    "timestamp": timestamp,
                    "final_submit": True,
                })
            _merge_client_activity_logs(session_log, request.activity_logs)
        
        detected_language = detect_programming_language(code)
        submission_id = f"{user_id}_{assignment_key}_{uuid.uuid4().hex[:8]}"
        
        # Log submission details
        logger.info(f"CODE SUBMISSION - User: {user_id}, Assignment: {assignment_key}, Session: {session_id}")
        
        # FIREBASE SAVE - Upload complete session with all logged data
        firebase_status = "not_attempted"
        if session_log:
            try:
                # Save complete session data to Firebase
                success = await save_session_to_firebase(session_log, suggestion_clicked, compiler_clicked, programming_language, request.survey_data)
                firebase_status = "success" if success else "failed"

                # Update local backup with final data
                save_session_to_local(session_log)
                
                # Remove from active sessions - FORCE LOGOUT
                if session_id:
                    del active_sessions[session_id]
                
            except Exception as firebase_error:
                logger.error(f"Firebase submission save failed: {firebase_error}")
                firebase_status = "failed"
                
                # Still remove from active sessions even if Firebase fails
                if session_id and session_id in active_sessions:
                    del active_sessions[session_id]
        
        # Save submission file locally as backup
        try:
            submissions_dir = Path(os.getenv("SUBMISSIONS_DIR", "submissions"))
            submissions_dir.mkdir(exist_ok=True)
            
            extensions = {
                "javascript": ".js", "python": ".py", "java": ".java",
                "cpp": ".cpp", "csharp": ".cs", "html": ".html",
                "css": ".css", "sql": ".sql"
            }
            extension = extensions.get(detected_language, ".txt")
            
            if submitted_files and len(submitted_files) > 1:
                sub_dir = submissions_dir / submission_id
                sub_dir.mkdir(exist_ok=True)
                meta_file = sub_dir / "_submission_info.txt"
                with open(meta_file, 'w', encoding='utf-8') as f:
                    f.write(f"User ID: {user_id}\n")
                    f.write(f"Assignment Key: {assignment_key}\n")
                    f.write(f"Session ID: {session_id}\n")
                    f.write(f"Submission ID: {submission_id}\n")
                    f.write(f"Detected Language: {detected_language}\n")
                    f.write(f"Timestamp: {timestamp}\n")
                    f.write(f"Firebase Status: {firebase_status}\n")
                    f.write(f"Files: {', '.join(sf['filename'] for sf in submitted_files)}\n")
                for sf in submitted_files:
                    sf_path = sub_dir / sf["filename"]
                    with open(sf_path, 'w', encoding='utf-8') as f:
                        f.write(sf["content"])
            else:
                submission_file = submissions_dir / f"{submission_id}{extension}"
                with open(submission_file, 'w', encoding='utf-8') as f:
                    f.write(f"# Submission Details\n")
                    f.write(f"# User ID: {user_id}\n")
                    f.write(f"# Assignment Key: {assignment_key}\n")
                    f.write(f"# Session ID: {session_id}\n")
                    f.write(f"# Submission ID: {submission_id}\n")
                    f.write(f"# Detected Language: {detected_language}\n")
                    f.write(f"# Timestamp: {timestamp}\n")
                    f.write(f"# Firebase Status: {firebase_status}\n")
                    f.write(f"#{'='*50}\n\n")
                    f.write(code)
            
        except Exception as save_error:
            logger.error(f"Failed to save submission file: {save_error}")
        
        # NEW: Send notification email
        try:
            email_data = {
                'user_id': user_id,
                'assignment_key': assignment_key,
                'session_id': session_id,
                'condition': session_log.condition if session_log else 'unknown',
                'programming_language': programming_language,
                'timestamp': timestamp,
                'submission_id': submission_id,
                'code': code,
                'suggestion_clicked': suggestion_clicked,
                'compiler_clicked': compiler_clicked,
                'firebase_status': firebase_status
            }
            
            # Send email asynchronously to avoid blocking the response
            import asyncio
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, send_notification_email, email_data)
            
            logger.info(f"Email notification queued for submission {submission_id}")
            
        except Exception as email_error:
            logger.error(f"Failed to send email notification: {email_error}")
            # Don't fail the submission if email fails
        
        return SubmitResponse(
            status="success",
            message=f"Code submitted successfully! Session ended. Please start a new session to continue.",
            submission_id=submission_id,
            timestamp=timestamp,
            firebase_status=firebase_status,
            logout_required=True
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in code submission: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Submission failed: {str(e)}")

# Set up templates directory
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Redirect to home page for consent and authentication"""
    return RedirectResponse(url="/home.html", status_code=302)

@app.get("/home.html", response_class=HTMLResponse)
@app.get("/home", response_class=HTMLResponse)
async def serve_home(request: Request):
    """Serve the home login page with consent form"""
    return templates.TemplateResponse("home.html", {"request": request})

@app.get("/index.html", response_class=HTMLResponse)
async def serve_ide(request: Request):
    """
    Serve the IDE page - this handles the unique URLs
    Users should only access this with proper session parameters
    """
    # Check if there are session parameters in the URL
    session_token = request.query_params.get("session")
    user_id = request.query_params.get("user")
    
    if not session_token or not user_id:
        # Redirect to home page if no session parameters
        logger.warning("Direct access to IDE without session parameters - redirecting to home")
        return RedirectResponse(url="/home.html", status_code=302)
    
    # Serve the IDE page - authentication will be handled by JavaScript
    return templates.TemplateResponse("index.html", {
    "request": request,
    "session_token": session_token,
    "user_id": user_id,
        "side_panel_hidden": get_side_panel_hidden_assignment_keys(),
        "teaser_assignment": os.getenv("TEASER", "")
    })

@app.get("/health")
async def health_check():
    firebase_status = "connected" if firebase_db else "disconnected"
    return {
        "status": "healthy",
        "service": "AI Code Assistant with Firebase Integration",
        "firebase_status": firebase_status,
        "active_sessions": len(active_sessions),
        "environment": {
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
            "firebase_configured": bool(firebase_db),
            "max_code_length": os.getenv("MAX_CODE_LENGTH", "10000"),
            "max_submission_length": os.getenv("MAX_SUBMISSION_LENGTH", "15000")
        }
    }

@app.get("/stats")
async def get_stats():
    """Get API statistics"""
    firebase_status = "connected" if firebase_db else "disconnected"
    return {
        "api_status": "operational",
        "firebase_status": firebase_status,
        "active_sessions": len(active_sessions),
        "openai_model": "gpt-4o-mini",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "supported_languages": [
            "javascript", "python", "java", "cpp", "csharp", 
            "html", "css", "sql", "and more"
        ],
        "features": [
            "firebase_integration",
            "hierarchical_data_structure", 
            "submission_only_saves",
            "local_logging_backup",
            "session_management",
            "condition_based_tracking",
            "language_agnostic_analysis",
            "multi_language_support",
            "ai_guess_history",
            "full_code_suggestions", 
            "click_tracking",
            "strict_assignment_validation",
            "automatic_logout_on_submit"
        ],
        "configuration": {
            "cors_origins": os.getenv("CORS_ORIGINS", "*"),
            "api_host": os.getenv("API_HOST", "0.0.0.0"),
            "api_port": int(os.getenv("API_PORT", "8000")),
            "condition_1_prefixes": os.getenv("CONDITION_1_PREFIXES", "Condition1_all"),
            "condition_2_prefixes": os.getenv("CONDITION_2_PREFIXES", "Condition2_partial,fdasdfsa"),
            "condition_3_prefixes": os.getenv("CONDITION_3_PREFIXES", "Condition3_suggestions"),
            "submissions_dir": os.getenv("SUBMISSIONS_DIR", "submissions")
        }
    }

@app.get("/debug/firebase")
async def debug_firebase():
    """Debug Firebase connection"""
    return {
        "firebase_db_type": str(type(firebase_db)),
        "firebase_db_exists": firebase_db is not None,
        "firebase_apps_count": len(firebase_admin._apps),
        "environment_vars": {
            "FIREBASE_PROJECT_ID": os.getenv("FIREBASE_PROJECT_ID"),
            "FIREBASE_PRIVATE_KEY_set": bool(os.getenv("FIREBASE_PRIVATE_KEY")),
            "FIREBASE_DATABASE_URL": os.getenv("FIREBASE_DATABASE_URL"),
            "FIREBASE_CLIENT_EMAIL": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "GOOGLE_APPLICATION_CREDENTIALS": bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")),
        }
    }

@app.get("/debug/test-write")
async def test_firebase_write():
    """Test writing to Firebase"""
    if not firebase_db:
        return {"error": "Firebase not initialized"}
    
    try:
        test_data = {
            'test_message': 'Hello Firebase from API!',
            'timestamp': datetime.now().isoformat(),
            'test_id': str(uuid.uuid4())
        }
        
        test_ref = firebase_db.child('api_tests').push()
        test_ref.set(test_data)
        
        return {
            "success": "Test data written to Firebase successfully",
            "test_data": test_data,
            "firebase_path": f"api_tests/{test_ref.key}"
        }
    except Exception as e:
        return {
            "error": str(e), 
            "error_type": type(e).__name__,
            "firebase_db_type": str(type(firebase_db))
        }
    

@app.get("/survey-config", response_model=SurveyConfig)
async def get_survey_config_endpoint():
    """Get periodic survey configuration from environment variables"""
    try:
        # Get questions from environment variables
        questions = []
        question_num = 1
        while True:
            question = os.getenv(f"Q{question_num}")
            if question:
                questions.append(question.strip())
                question_num += 1
            else:
                break
        
        # Default questions if none found in env
        if not questions:
            questions = [
                "I trust the AI's responses.",
                "I am confident in the AI's responses.",
                "I am suspicious of the AI's intentions.",
                "The AI's responses are consistent."
            ]
        
        feedback_interval = int(os.getenv("FEEDBACK_INTERVAL", "4"))
        
        logger.info(f"Survey config: {len(questions)} questions, interval: {feedback_interval}")
        
        return SurveyConfig(
            questions=questions,
            feedback_interval=feedback_interval
        )
        
    except Exception as e:
        logger.error(f"Error getting survey config: {e}")
        # Return defaults
        return SurveyConfig(
            questions=[
                "I trust the AI's responses.",
                "I am confident in the AI's responses.",
                "I am suspicious of the AI's intentions.",
                "The AI's responses are consistent."
            ],
            feedback_interval=4
        )
    

class ActivityLogRequest(BaseModel):
    activities: List[ActivityEntry]
    session_id: str


class EditorCheckpointRequest(BaseModel):
    """Idempotent server-side editor draft (coding phase durability)."""
    session_id: str
    user_id: str
    assignment_key: str
    code: str = ""
    programming_language: str = "javascript"
    cursor_line: Optional[int] = None
    cursor_column: Optional[int] = None
    scroll_top: Optional[float] = None
    current_problem_index: Optional[int] = None


@app.post("/checkpoint-editor")
async def checkpoint_editor(request: EditorCheckpointRequest):
    """
    Persist current editor content to Firebase (sessions/{session_id}/editor_draft)
    and to local session_logs. Full session Firebase checkpoint is throttled.
    """
    max_len = int(os.getenv("MAX_SUBMISSION_LENGTH", "150000"))
    if len(request.code) > max_len:
        raise HTTPException(status_code=400, detail=f"Code too long (max {max_len} chars)")

    session_id = request.session_id.strip()
    uid = request.user_id.strip()
    aid = request.assignment_key.strip()
    if not session_id or not uid:
        raise HTTPException(status_code=400, detail="session_id and user_id are required")

    session_log = active_sessions.get(session_id)
    if session_log is not None:
        if session_log.user_id not in ("unknown", uid) and session_log.user_id != uid:
            raise HTTPException(status_code=403, detail="user_id does not match this session")
        session_log.editor_draft_checkpoint = request.code
        session_log.editor_draft_updated_at = datetime.now(timezone.utc).isoformat()
        if aid and session_log.assignment_id == "unknown":
            session_log.assignment_id = aid
        save_session_to_local(session_log)

    firebase_editor_ok = False
    if firebase_db:
        payload = {
            "user_id": uid,
            "assignment_id": aid,
            "programming_language": request.programming_language,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "code": request.code,
            "cursor_line": request.cursor_line,
            "cursor_column": request.cursor_column,
            "scroll_top": request.scroll_top,
            "current_problem_index": request.current_problem_index,
        }
        try:
            clean = json.loads(json.dumps(payload, default=str))
            ref = firebase_db.child("sessions").child(session_id).child("editor_draft")
            firebase_editor_ok = firebase_rtdb_set_with_retry(ref, clean, max_attempts=3)
        except Exception as e:
            logger.error("Firebase editor_draft set failed: %s", e)

    firebase_full = "skipped"
    if session_log is not None:
        now = time.time()
        last = _last_editor_full_checkpoint_at.get(session_id, 0.0)
        if now - last >= EDITOR_FULL_CHECKPOINT_SEC:
            try:
                ok = await asyncio.to_thread(
                    lambda: checkpoint_session_to_firebase(session_log, "editor_sync", force=True)
                )
                firebase_full = "ok" if ok else "failed"
                if ok:
                    _last_editor_full_checkpoint_at[session_id] = now
            except Exception as e:
                logger.warning("Full Firebase checkpoint after editor sync failed: %s", e)
                firebase_full = "error"

    return {
        "status": "success",
        "firebase_editor_draft": firebase_editor_ok,
        "firebase_full_checkpoint": firebase_full,
        "session_known": session_log is not None,
    }


@app.post("/log-activities")
async def log_activities(request: ActivityLogRequest):
    """Log user activities during the session"""
    try:
        session_id = request.session_id
        
        if not session_id:
            raise HTTPException(status_code=400, detail="Session ID is required")
        
        # Validate that activities list is not empty
        if not request.activities:
            logger.warning(f"Empty activities list for session {session_id}")
            return {
                "status": "success",
                "logged_activities": 0,
                "message": "No activities to log",
                "firebase_checkpoint": "n/a",
            }
        
        if session_id not in active_sessions:
            logger.warning(f"Session {session_id} not found in active sessions - creating minimal session for logging")
            active_sessions[session_id] = SessionLog(
                user_id="unknown",
                assignment_id="unknown", 
                session_id=session_id,
                condition=0,
                code_start_timestamp=datetime.now().isoformat(),
                activity_log=[],
                activity_summary=ActivitySummary()
            )
        
        session_log = active_sessions[session_id]
        
        # Add activities to session log
        session_log.activity_log.extend(request.activities)
        
        # Update activity summary
        for activity in request.activities:
            activity_type = activity.activity_type
            try:
                # Code execution activities
                if activity_type == "code_execution":
                    session_log.activity_summary.total_code_executions += 1
                
                # New enhanced editing activities
                elif activity_type in ["code_editing_insert", "code_editing_delete", "code_editing_modify"]:
                    session_log.activity_summary.total_keystrokes += 1
                    # Update peak lines safely
                    if isinstance(activity.session_context, dict) and "total_lines" in activity.session_context:
                        try:
                            total_lines = int(activity.session_context["total_lines"])
                            session_log.activity_summary.peak_code_lines = max(
                                session_log.activity_summary.peak_code_lines,
                                total_lines
                            )
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid total_lines value: {activity.session_context.get('total_lines')}")
                
                # Suggestion and history interactions
                elif activity_type == "suggestion_interaction":
                    session_log.activity_summary.total_suggestion_clicks += 1
                elif activity_type == "history_interaction":
                    session_log.activity_summary.total_history_views += 1
                elif activity_type == "history_check":
                    session_log.activity_summary.total_history_views += 1

                # AI analysis activities
                elif activity_type == "ai_analysis_trigger":
                    session_log.activity_summary.total_ai_analyses += 1
                elif activity_type == "ai_analysis_complete":
                    # Don't double count - already counted in trigger
                    pass
                # Idle tracking
                elif activity_type == "idle":
                    pass  # Just log, no summary update needed

                # Page unload attempts
                elif activity_type == "page_unload_attempt":
                    pass  # Security logging, no summary update needed
                
                # UI interaction activities
                elif activity_type == "font_size_change":
                    session_log.activity_summary.font_changes += 1
                elif activity_type == "terminal_clear":
                    session_log.activity_summary.terminal_clears += 1
                
                # Survey activities (simplified)
                elif activity_type == "survey_interaction":
                    action = activity.activity_details.get("action")
                    if action == "survey_completed":
                        session_log.activity_summary.surveys_completed += 1
                    # Only log survey start/end, ignore other survey variants
                
                # Tab switching activities
                elif activity_type == "tab_switching":
                    if activity.activity_details.get("switch_type") == "tab_return":
                        # Track away time safely
                        away_duration = activity.activity_details.get("away_duration_ms", 0)
                        try:
                            away_duration = float(away_duration) / 1000  # Convert to seconds
                            session_log.activity_summary.away_time_seconds += away_duration
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid away_duration_ms value: {away_duration}")
                
                # Session management activities
                elif activity_type in ["session_start", "session_end"]:
                    pass  # These are tracked differently
                
                # Idle and focus tracking
                elif activity_type == "idle":
                    pass  # Handled by duration tracking
                
                # Paste attempts
                elif activity_type == "paste_attempt":
                    pass  # Security logging, no summary update needed
                
                # Page unload attempts
                elif activity_type == "page_unload_attempt":
                    pass  # Security logging
                
                # Legacy activity types (keep for backward compatibility)
                elif activity_type == "code_edit":
                    session_log.activity_summary.total_keystrokes += 1
                elif activity_type == "suggestion_click":
                    session_log.activity_summary.total_suggestion_clicks += 1
                elif activity_type == "history_view":
                    session_log.activity_summary.total_history_views += 1
                elif activity_type == "tab_visibility_change":
                    if activity.activity_details.get("visibility_state") == "visible":
                        # Track away time safely
                        away_duration = activity.activity_details.get("away_duration_ms", 0)
                        try:
                            away_duration = float(away_duration) / 1000  # Convert to seconds
                            session_log.activity_summary.away_time_seconds += away_duration
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid away_duration_ms value: {away_duration}")
                
                # Execution output activities
                elif activity_type == "execution_output":
                    pass  # Already counted in code_execution
                
                # Unknown activity types
                else:
                    logger.debug(f"Unknown activity type: {activity_type}")
                    
            except Exception as activity_error:
                logger.error(f"Error processing activity {activity_type}: {activity_error}")
                # Continue processing other activities
        
        # Calculate focus time safely
        try:
            import time
            current_time = time.time()
            session_start_time = datetime.fromisoformat(session_log.code_start_timestamp.replace('Z', '+00:00')).timestamp()
            total_session_time = current_time - session_start_time
            session_log.activity_summary.focus_time_seconds = max(0, total_session_time - session_log.activity_summary.away_time_seconds)
        except Exception as time_error:
            logger.warning(f"Could not calculate focus time: {time_error}")
        
        # Save updated session locally
        save_session_to_local(session_log)

        firebase_checkpoint = "skipped"
        try:
            ck = await asyncio.to_thread(
                lambda: checkpoint_session_to_firebase(session_log, "activity_batch", force=False)
            )
            firebase_checkpoint = "ok" if ck else "failed"
        except Exception as fb_e:
            logger.warning("Firebase activity checkpoint error: %s", fb_e)
            firebase_checkpoint = "error"

        return {
            "status": "success",
            "logged_activities": len(request.activities),
            "firebase_checkpoint": firebase_checkpoint,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Activity logging error: {str(e)}")
        return {"status": "error", "message": f"Activity logging failed: {str(e)}", "logged_activities": 0}
    


@app.post("/submit-survey")
async def submit_survey(request: SurveySubmission):
    """Submit survey responses with AI history context"""
    try:
        # Get session log
        session_log = None
        if request.session_id in active_sessions:
            session_log = active_sessions[request.session_id]
        
        # Enhance request with AI history context if not provided
        if not request.ai_history_context and session_log:
            request.ai_history_context = extract_ai_history_context(session_log)
        
        # Save survey data locally with AI history context
        surveys_dir = Path(os.getenv("SUBMISSIONS_DIR", "submissions")) / "surveys"
        surveys_dir.mkdir(exist_ok=True)
        
        survey_file = surveys_dir / f"{request.session_id}_{request.survey_type}_{request.analysis_count}.json"
        with open(survey_file, 'w', encoding='utf-8') as f:
            json.dump(request.dict(), f, indent=2, default=str)
        
        logger.info(f"Survey with AI history saved: {request.user_id} - {request.survey_type} survey at analysis {request.analysis_count}")

        # Firebase: same path as /log-periodic-survey (incremental, not only at final submit)
        if request.survey_type in ("periodic", "final"):
            fb_payload: Dict[str, Any] = {
                "survey_type": request.survey_type,
                "analysis_count": request.analysis_count,
                "timestamp": request.timestamp,
                "user_id": request.user_id,
                "assignment_key": request.assignment_key,
                "responses": [r.dict() for r in request.responses],
            }
            if request.ai_history_context:
                fb_payload["ai_history_context"] = request.ai_history_context
            if append_periodic_survey_to_firebase(request.session_id, fb_payload):
                logger.info("Periodic/final survey appended to Firebase for session %s", request.session_id)
            if request.session_id in active_sessions:
                session_log = active_sessions[request.session_id]
                session_log.periodic_surveys.append(fb_payload)
                save_session_to_local(session_log)
                try:
                    await asyncio.to_thread(
                        lambda: checkpoint_session_to_firebase(
                            session_log, "periodic_survey_submit", force=True
                        )
                    )
                except Exception as fb_e:
                    logger.warning("Firebase checkpoint after submit-survey failed: %s", fb_e)
        
        return {
            "status": "success",
            "message": f"{request.survey_type.title()} survey saved successfully",
            "timestamp": request.timestamp,
            "ai_history_entries": len(request.ai_history_context.get('ai_guess_history', []) if request.ai_history_context else [])
        }
        
    except Exception as e:
        logger.error(f"Survey submission error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Survey submission failed: {str(e)}")

def extract_ai_history_context(session_log: SessionLog) -> Dict[str, Any]:
    """Extract AI history context for survey submission"""
    context = {
        'condition': session_log.condition,
        'total_ai_analyses': len(session_log.ai_guess_history),
        'ai_guess_history': [],
        'timestamp_range': {
            'start': session_log.code_start_timestamp,
            'survey_time': datetime.now().isoformat()
        }
    }
    
    # Add AI guess history (available for all conditions)
    for entry in session_log.ai_guess_history:
        context['ai_guess_history'].append({
            'timestamp': entry.timestamp,
            'problem_analysis': entry.problem_analysis,
            'next_line_suggestion': entry.next_line_suggestion,
            'analysis_type': entry.analysis_type
        })
    
    # Add condition-specific history
    if session_log.condition == 1 and session_log.correct_description_history:
        context['correct_description_history'] = []
        for entry in session_log.correct_description_history:
            context['correct_description_history'].append({
                'timestamp': entry.timestamp,
                'problem_analysis': entry.problem_analysis,
                'next_line_suggestion': entry.next_line_suggestion,
                'analysis_type': entry.analysis_type
            })
    
    elif session_log.condition == 3 and session_log.suggestions_only_history:
        context['suggestions_only_history'] = []
        for entry in session_log.suggestions_only_history:
            context['suggestions_only_history'].append({
                'timestamp': entry.timestamp,
                'next_line_suggestion': entry.next_line_suggestion,
                'analysis_type': entry.analysis_type
            })
    
    return context

# ADD THIS NEW ENUM
class SurveyStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    NOT_REQUIRED = "not_required"


# ADD THESE NEW MODELS

class SurveyQuestionData(BaseModel):
    """Store complete question data"""
    id: str
    type: str  # likert, multiple_choice, open_ended, info_block
    question: str
    required: bool
    # Type-specific fields
    scale: Optional[Dict[str, Any]] = None  # For likert questions
    options: Optional[List[str]] = None      # For multiple choice
    placeholder: Optional[str] = None        # For open-ended
    max_length: Optional[int] = None         # For open-ended
    min_length: Optional[int] = None         # For open-ended (minimum character requirement)
    help_text: Optional[str] = None          # Optional help text for any question type


class SurveyResponseData(BaseModel):
    """Store response with question context"""
    question_id: str
    question_text: str
    question_type: str
    question_config: Dict[str, Any]  # Full question configuration
    answer: Any  # Can be int, str, or None
    required: bool


class PrePostSurveySubmission(BaseModel):
    user_id: str
    session_id: str
    assignment_key: str
    survey_type: str  # 'pre' or 'post'
    survey_title: str
    survey_description: str
    questions: List[SurveyQuestionData]
    responses: List[SurveyResponseData]
    timestamp: str
    
    # Metadata
    total_questions: int = 0
    required_questions: int = 0
    completion_time_seconds: Optional[float] = None


class SurveyStatusResponse(BaseModel):
    pre_survey_status: str
    post_survey_status: str
    pre_survey_timestamp: Optional[str] = None
    post_survey_timestamp: Optional[str] = None
    can_access_ide: bool
    can_submit_code: bool


@app.get("/survey-config/{assignment_key}")
async def get_survey_config(assignment_key: str, problem_index: int = 0):
    """
    Get pre, post, and periodic survey configuration for an assignment key
    FULLY CASE-INSENSITIVE - works with any case variation
    Also supports instructor-created quizzes (assignment_key starts with 'quiz_')
    """
    try:
        # ── Instructor-created quiz: load surveys from Firebase ──
        if assignment_key.startswith("quiz_") and firebase_db:
            access_code = assignment_key[5:]  # strip "quiz_"
            # Look up access code to get actual quiz UUID
            access_ref = fb_dict(db.reference(f"quiz_access/{access_code}").get())
            quiz_data = None
            if access_ref and access_ref.get("quiz_id"):
                quiz_data = fb_dict(db.reference(f"quizzes/{access_ref['quiz_id']}").get())
            if quiz_data:
                quiz_data = normalize_quiz_data(quiz_data)
                problems = quiz_data.get("problems", [])
                idx = min(problem_index, len(problems) - 1) if problems else 0
                problem = problems[idx] if problems else {}

                pre_enabled = problem.get("pre_survey_enabled", False)
                post_enabled = problem.get("post_survey_enabled", False)
                rec_enabled = problem.get("recurrent_survey_enabled", False)

                return {
                    "assignment_key": assignment_key,
                    "problem_index": idx,
                    "pre_survey": {
                        "title": "Pre-Study Survey",
                        "description": "Please answer the following questions before starting.",
                        "estimated_time_minutes": 5,
                        "enabled": pre_enabled,
                        "questions": problem.get("pre_survey", []) if pre_enabled else [],
                    },
                    "post_survey": {
                        "title": "Post-Study Survey",
                        "description": "Thank you for completing the task! Please share your experience.",
                        "estimated_time_minutes": 10,
                        "enabled": post_enabled,
                        "questions": problem.get("post_survey", []) if post_enabled else [],
                    },
                    "periodic_survey": {
                        "enabled": rec_enabled and bool(problem.get("recurrent_survey")),
                        "interval": int(problem.get("recurrent_survey_interval", 4)),
                        "start": int(problem.get("recurrent_survey_start", 5)),
                        "title": "Quick Check-in",
                        "description": "Please rate your current experience",
                        "questions": problem.get("recurrent_survey", []) if rec_enabled else [],
                    },
                }
            # Fall through to empty config if quiz not found
            return {
                "assignment_key": assignment_key,
                "pre_survey": {"title": "", "description": "", "questions": []},
                "post_survey": {"title": "", "description": "", "questions": []},
                "periodic_survey": {"enabled": False, "interval": 4, "questions": []},
            }

        # ── Standard file-based survey config ──
        # Get base key for file reading (this returns capitalized version)
        base_key = get_assignment_base_key(assignment_key)
        if not base_key:
            raise HTTPException(status_code=400, detail="Invalid assignment key")
        
        # FIXED: Use lowercase for file matching
        base_key_lower = base_key.lower()
        
        # Case-insensitive survey file search
        surveys_dir = Path("surveys")
        survey_file = None
        
        if surveys_dir.exists():
            try:
                for file in surveys_dir.iterdir():
                    # FIXED: Compare lowercase to lowercase
                    if file.is_file() and file.stem.lower() == base_key_lower and file.suffix == '.json':
                        survey_file = file
                        logger.info(f"Found survey config: {file} (matched with key: {base_key})")
                        break
            except Exception as e:
                logger.error(f"Error searching for survey config: {e}")
        
        if not survey_file or not survey_file.exists():
            # Fallback to default survey
            survey_file = Path("surveys") / "default_survey.json"
            logger.warning(f"Survey config not found for {base_key} (tried: {base_key_lower}.json), using default")
        
        if not survey_file.exists():
            # Return empty config if no default exists
            logger.error("No survey configuration found - neither specific nor default")
            return {
                "assignment_key": base_key,
                "pre_survey": {"title": "", "description": "", "questions": []},
                "post_survey": {"title": "", "description": "", "questions": []},
                "periodic_survey": {"enabled": False, "interval": 4, "questions": []}
            }
        
        with open(survey_file, 'r', encoding='utf-8') as f:
            survey_config = json.load(f)
        
        logger.info(f"Successfully loaded survey config for {base_key} from {survey_file.name}")
        return survey_config
        
    except Exception as e:
        logger.error(f"Error loading survey config: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load survey config: {str(e)}")
    

@app.get("/survey-status/{session_id}")
async def get_survey_status(session_id: str):
    """
    Get current survey completion status for a session
    """
    try:
        if session_id not in active_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        
        session_log = active_sessions[session_id]
        
        # Check if surveys are required for this assignment
        base_key = get_assignment_base_key(session_log.assignment_id)
        survey_file = Path("surveys") / f"{base_key}.json"
        
        has_surveys = survey_file.exists()
        pre_required = False
        post_required = False
        
        if has_surveys:
            try:
                with open(survey_file, 'r', encoding='utf-8') as f:
                    survey_config = json.load(f)
                    pre_required = len(survey_config.get('pre_survey', {}).get('questions', [])) > 0
                    post_required = len(survey_config.get('post_survey', {}).get('questions', [])) > 0
            except Exception as e:
                logger.error(f"Error loading survey config: {e}")
        
        # Determine access permissions
        can_access_ide = (
            not pre_required or 
            session_log.pre_survey_status == SurveyStatus.COMPLETED
        )
        
        can_submit_code = (
            not post_required or 
            session_log.post_survey_status == SurveyStatus.COMPLETED
        )
        
        return SurveyStatusResponse(
            pre_survey_status=session_log.pre_survey_status,
            post_survey_status=session_log.post_survey_status,
            pre_survey_timestamp=session_log.pre_survey_timestamp,
            post_survey_timestamp=session_log.post_survey_timestamp,
            can_access_ide=can_access_ide,
            can_submit_code=can_submit_code
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting survey status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/update-survey-status")
async def update_survey_status(
    session_id: str, 
    survey_type: str,
    status: str
):
    """
    Update survey status for a session
    """
    try:
        if session_id not in active_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        
        session_log = active_sessions[session_id]
        timestamp = datetime.now().isoformat()
        
        if survey_type == 'pre':
            session_log.pre_survey_status = status
            if status == SurveyStatus.COMPLETED or status == SurveyStatus.IN_PROGRESS:
                session_log.pre_survey_timestamp = timestamp
        elif survey_type == 'post':
            session_log.post_survey_status = status
            if status == SurveyStatus.COMPLETED or status == SurveyStatus.IN_PROGRESS:
                session_log.post_survey_timestamp = timestamp
        else:
            raise HTTPException(status_code=400, detail="Invalid survey type")
        
        # Save updated session to local backup
        save_session_to_local(session_log)

        try:
            await asyncio.to_thread(
                lambda: checkpoint_session_to_firebase(
                    session_log, f"survey_status_{survey_type}", force=True
                )
            )
        except Exception as fb_e:
            logger.warning("Firebase checkpoint after survey status failed: %s", fb_e)
        
        logger.info(f"Survey status updated: {session_id} - {survey_type} - {status}")
        
        return {
            "status": "success",
            "message": f"{survey_type.title()} survey status updated to {status}",
            "timestamp": timestamp
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating survey status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/submit-pre-post-survey")
async def submit_pre_post_survey(request: PrePostSurveySubmission):
    """Submit pre or post survey with complete question data and responses"""
    try:
        # Update session log with complete survey data
        if request.session_id in active_sessions:
            session_log = active_sessions[request.session_id]
            
            # Create comprehensive survey data package
            survey_package = {
                'survey_title': request.survey_title,
                'survey_description': request.survey_description,
                'total_questions': request.total_questions or len(request.questions),
                'required_questions': request.required_questions or sum(1 for q in request.questions if q.required),
                'completion_time_seconds': request.completion_time_seconds,
                'timestamp': request.timestamp,
                
                # Complete questions as presented to user
                'questions': [
                    {
                        'id': q.id,
                        'type': q.type,
                        'question': q.question,
                        'required': q.required,
                        'scale': q.scale if q.scale else None,
                        'options': q.options if q.options else None,
                        'placeholder': q.placeholder if q.placeholder else None,
                        'max_length': q.max_length if q.max_length else None
                    } for q in request.questions
                ],
                
                # User responses with full context
                'responses': [
                    {
                        'question_id': r.question_id,
                        'question_text': r.question_text,
                        'question_type': r.question_type,
                        'question_config': r.question_config,
                        'answer': r.answer,
                        'required': r.required,
                        'answered': r.answer is not None and r.answer != ''
                    } for r in request.responses
                ]
            }
            
            # Add AI context for post-survey
            if request.survey_type == 'post':
                survey_package['ai_context'] = {
                    'total_analyses': len(session_log.ai_guess_history),
                    'suggestion_clicks': len([entry for entry in session_log.activity_log if entry.activity_type == 'suggestion_interaction']),
                    'code_executions': len([entry for entry in session_log.activity_log if entry.activity_type == 'code_execution']),
                    'ai_guess_history_summary': [
                        {
                            'timestamp': entry.timestamp,
                            'problem_analysis': entry.problem_analysis[:100] + '...' if len(entry.problem_analysis) > 100 else entry.problem_analysis
                        } for entry in session_log.ai_guess_history[-5:]
                    ]
                }
            
            # Store in session log
            if request.survey_type == 'pre':
                session_log.pre_survey_status = SurveyStatus.COMPLETED
                session_log.pre_survey_timestamp = request.timestamp
                session_log.pre_survey_responses = survey_package
            elif request.survey_type == 'post':
                session_log.post_survey_status = SurveyStatus.COMPLETED
                session_log.post_survey_timestamp = request.timestamp
                session_log.post_survey_responses = survey_package
            
            # Save updated session
            save_session_to_local(session_log)
            try:
                await asyncio.to_thread(
                    lambda: checkpoint_session_to_firebase(
                        session_log, f"survey_{request.survey_type}_complete", force=True
                    )
                )
            except Exception as fb_e:
                logger.warning("Firebase checkpoint after pre/post survey failed: %s", fb_e)
        
        # Save survey data to separate file (backup)
        surveys_dir = Path(os.getenv("SUBMISSIONS_DIR", "submissions")) / "pre_post_surveys"
        surveys_dir.mkdir(exist_ok=True, parents=True)
        
        session_id_short = request.session_id.split('_')[-1][:8] if '_' in request.session_id else request.session_id[:8]
        survey_file = surveys_dir / f"{request.user_id}_{request.assignment_key}_{session_id_short}_{request.survey_type}.json"
        
        # Prepare complete data for file storage
        file_data = {
            'user_id': request.user_id,
            'session_id': request.session_id,
            'assignment_key': request.assignment_key,
            'survey_type': request.survey_type,
            'survey_title': request.survey_title,
            'survey_description': request.survey_description,
            'timestamp': request.timestamp,
            'total_questions': request.total_questions or len(request.questions),
            'required_questions': request.required_questions or sum(1 for q in request.questions if q.required),
            'completion_time_seconds': request.completion_time_seconds,
            'questions': [q.dict() for q in request.questions],
            'responses': [r.dict() for r in request.responses]
        }
        
        with open(survey_file, 'w', encoding='utf-8') as f:
            json.dump(file_data, f, indent=2, default=str)

        if request.session_id not in active_sessions:
            try:
                await asyncio.to_thread(
                    lambda: checkpoint_pre_post_survey_file_payload_to_firebase(
                        request.session_id,
                        request.user_id,
                        request.assignment_key,
                        request.survey_type,
                        file_data,
                    )
                )
            except Exception as fb_e:
                logger.warning("Firebase orphan survey checkpoint failed: %s", fb_e)
        
        logger.info(f"{request.survey_type.upper()} survey saved: {request.user_id} - {survey_file.name}")
        
        return {
            "status": "success",
            "message": f"{request.survey_type.title()} survey saved successfully",
            "timestamp": request.timestamp,
            "file": survey_file.name,
            "questions_saved": len(request.questions),
            "responses_saved": len(request.responses)
        }
        
    except Exception as e:
        logger.error(f"Survey submission error: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Survey submission failed: {str(e)}")
    
@app.post("/authenticate", response_model=AuthResponse)
async def authenticate_user(request: AuthRequest):
    """
    Authenticate user and return their assignment condition
    Creates a new session and starts logging
    """
    try:
        user_id = request.user_id.strip()
        assignment_key = request.assignment_key.strip()
        programming_language = request.programming_language or "javascript"
        
        if len(user_id) < 3:
            raise HTTPException(status_code=400, detail="User ID must be at least 3 characters")
        
        if len(assignment_key) < 4:
            raise HTTPException(status_code=400, detail="Assignment Key must be at least 4 characters")
        
        condition = get_assignment_condition(assignment_key)
        
        if condition is None:
            raise HTTPException(status_code=400, detail="Invalid assignment key. Please check your credentials and try again.")
        
        session_id = f"{user_id}_{assignment_key}_{uuid.uuid4().hex[:8]}"
        
        problem_description = None
        if condition == 1:
            try:
                assignment_file = get_assignment_file_path(assignment_key)
                if assignment_file and assignment_file.exists():
                    with open(assignment_file, 'r', encoding='utf-8') as f:
                        problem_description = f.read().strip()
                    logger.info(f"Loaded problem description from: {assignment_file}")
                else:
                    logger.warning(f"Assignment file not found for key: {assignment_key}")
                    base_key = get_assignment_base_key(assignment_key)
                    problem_description = f"Assignment file not found for {base_key or assignment_key}"
            except Exception as e:
                logger.error(f"Error reading assignment file for {assignment_key}: {e}")
                problem_description = f"Error loading assignment description: {str(e)}"
        
        # Get base key from assignment_key (used for starter code and surveys)
        base_key = get_assignment_base_key(assignment_key)
        
        # Get starter code if available
        starter_code = get_starter_code(base_key, programming_language)
        
        # FIXED: Case-insensitive survey file check
        has_surveys = False
        survey_file = None
        if base_key:
            surveys_dir = Path("surveys")
            if surveys_dir.exists():
                base_key_lower = base_key.lower()
                for file in surveys_dir.iterdir():
                    if file.is_file() and file.stem.lower() == base_key_lower and file.suffix == '.json':
                        survey_file = file
                        has_surveys = True
                        break
        
        has_pre = False
        has_post = False
        if has_surveys and survey_file is not None:
            try:
                with open(survey_file, 'r', encoding='utf-8') as f:
                    survey_config = json.load(f)
                    has_pre = len(survey_config.get('pre_survey', {}).get('questions', [])) > 0
                    has_post = len(survey_config.get('post_survey', {}).get('questions', [])) > 0
            except Exception as e:
                logger.error(f"Error checking survey config: {e}")
        session_log = SessionLog(
            user_id=user_id,
            assignment_id=assignment_key,
            session_id=session_id,
            condition=condition,
            code_start_timestamp=datetime.now().isoformat(),
            ai_guess_history=[],
            correct_description_history=[] if condition == 1 else None,
            suggestions_only_history=[] if condition == 3 else None,
            problem_description=problem_description,
            pre_survey_status=SurveyStatus.NOT_REQUIRED if not has_pre else SurveyStatus.NOT_STARTED,
            post_survey_status=SurveyStatus.NOT_REQUIRED if not has_post else SurveyStatus.NOT_STARTED,
        )
        
        active_sessions[session_id] = session_log
        save_session_to_local(session_log)

        try:
            await asyncio.to_thread(
                lambda: checkpoint_session_to_firebase(session_log, "session_start", force=True)
            )
        except Exception as fb_e:
            logger.warning("Firebase checkpoint at auth failed: %s", fb_e)
        
        logger.info(f"Authentication successful: {user_id} -> Condition {condition}, Session: {session_id}, Language: {programming_language}")
        
        # Legacy auth: paste always allowed, tab tracking always on
        paste_allowed = 1
        tab_tracking_enabled = True
        
        # Check if periodic surveys are enabled for this assignment
        periodic_config = get_periodic_survey_config()
        periodic_enabled = base_key.lower() in periodic_config["enabled_prefixes"] if base_key else False
        
        # Check if final periodic survey is disabled for this assignment
        final_survey_disabled = is_final_survey_disabled(base_key)
        
        return AuthResponse(
            status="success",
            message="Authentication successful",
            condition=condition,
            problem_description=problem_description,
            session_id=session_id,
            paste_allowed=paste_allowed,
            tab_tracking_enabled=tab_tracking_enabled,
            has_pre_survey=has_pre,
            has_post_survey=has_post,
            programming_language=programming_language,
            starter_code=starter_code,
            periodic_survey_enabled=periodic_enabled,
            periodic_survey_start=periodic_config["start_after_suggestions"],
            final_survey_disabled=final_survey_disabled,
            current_problem_index=0,
            total_problems=1,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Authentication failed: {str(e)}")


@app.post("/log-periodic-survey")
async def log_periodic_survey(request: Request):
    """Log periodic survey responses during coding session"""
    try:
        data = await request.json()
        session_id = data.get('session_id')
        survey_data = data.get('survey_data')
        
        if not session_id or not survey_data:
            raise HTTPException(status_code=400, detail="Missing required data")
        
        # Log to Firebase if available
        if firebase_db:
            try:
                if append_periodic_survey_to_firebase(session_id, survey_data):
                    logger.info(
                        "Periodic survey #%s logged for session %s",
                        survey_data.get("survey_number"),
                        session_id,
                    )
                else:
                    logger.error("Firebase periodic_surveys write failed for session %s", session_id)
            except Exception as e:
                logger.error(f"Error logging to Firebase: {e}")
        
        # Also log locally
        if session_id in active_sessions:
            session_log = active_sessions[session_id]
            session_log.periodic_surveys.append(survey_data)
            save_session_to_local(session_log)
            try:
                await asyncio.to_thread(
                    lambda: checkpoint_session_to_firebase(session_log, "periodic_survey_batch", force=False)
                )
            except Exception as fb_e:
                logger.warning("Firebase checkpoint after periodic survey failed: %s", fb_e)
        
        return {"status": "success", "message": "Periodic survey logged"}
        
    except Exception as e:
        logger.error(f"Error logging periodic survey: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def save_session_to_firebase(
    session_log: SessionLog,
    suggestion_clicked: int,
    compiler_clicked: int,
    programming_language: str = "unknown",
    survey_data: Optional[Dict[str, Any]] = None,
):
    """Save complete session data to Firebase with surveys as separate top-level entries"""
    if not firebase_db:
        logger.warning("Firebase not available for saving session data")
        return False
    
    try:
        condition_path = f"condition_{session_log.condition}"
        session_metadata = {
            'user_id': session_log.user_id,
            'assignment_key': session_log.assignment_id,
            'session_id': session_log.session_id,
            'condition': session_log.condition,
            'programming_language': programming_language,
            'starting_time': session_log.code_start_timestamp,
            'end_time': session_log.code_submit_timestamp,
            'submitted_code': session_log.submitted_full_code,
            'submitted_files': session_log.submitted_files,
            'suggestion_clicked': suggestion_clicked,
            'compiler_clicked': compiler_clicked,
            'total_problems': session_log.total_problems,
            'per_problem_submissions': session_log.per_problem_submissions,
        }
        if session_log.quiz_id:
            session_metadata['quiz_id'] = session_log.quiz_id
        pre_survey = None
        if session_log.pre_survey_responses:
            pre_survey = {
                'status': session_log.pre_survey_status,
                'timestamp': session_log.pre_survey_timestamp,
                'completed': session_log.pre_survey_status == SurveyStatus.COMPLETED or session_log.pre_survey_status == "completed",
                'survey_title': session_log.pre_survey_responses.get('survey_title', 'Pre-Study Survey'),
                'survey_description': session_log.pre_survey_responses.get('survey_description', ''),
                'total_questions': session_log.pre_survey_responses.get('total_questions', 0),
                'required_questions': session_log.pre_survey_responses.get('required_questions', 0),
                'completion_time_seconds': session_log.pre_survey_responses.get('completion_time_seconds'),
                'questions': session_log.pre_survey_responses.get('questions', []),
                'responses': session_log.pre_survey_responses.get('responses', [])
            }
        post_survey = None
        if session_log.post_survey_responses:
            post_survey = {
                'status': session_log.post_survey_status,
                'timestamp': session_log.post_survey_timestamp,
                'completed': session_log.post_survey_status == SurveyStatus.COMPLETED or session_log.post_survey_status == "completed",
                'survey_title': session_log.post_survey_responses.get('survey_title', 'Post-Study Survey'),
                'survey_description': session_log.post_survey_responses.get('survey_description', ''),
                'total_questions': session_log.post_survey_responses.get('total_questions', 0),
                'required_questions': session_log.post_survey_responses.get('required_questions', 0),
                'completion_time_seconds': session_log.post_survey_responses.get('completion_time_seconds'),
                'questions': session_log.post_survey_responses.get('questions', []),
                'responses': session_log.post_survey_responses.get('responses', []),
                'ai_context': session_log.post_survey_responses.get('ai_context', {})
            }
        periodic_surveys_data = []
        
        # Log what we received
        logger.info(f"Processing session for Firebase save:")
        logger.info(f"  - User: {session_log.user_id}")
        logger.info(f"  - Session: {session_log.session_id}")
        logger.info(f"  - Survey data received: {bool(survey_data)}")

        if survey_data:
            logger.info(f"  - Survey data keys: {list(survey_data.keys())}")
            logger.info(f"  - Periodic surveys: {len(survey_data.get('periodic_surveys', []))}")
            
            # Log the actual content if it exists
            if survey_data.get('periodic_surveys'):
                logger.info(f"  - First survey sample: {survey_data['periodic_surveys'][0] if survey_data['periodic_surveys'] else 'empty'}")
        else:
            logger.warning("  - NO SURVEY DATA RECEIVED!")
        
        # Get periodic survey questions from environment
        periodic_survey_questions = []
        question_num = 1
        while True:
            question = os.getenv(f"Q{question_num}")
            if question:
                periodic_survey_questions.append(question.strip())
                question_num += 1
            else:
                break
        
        logger.info(f"  - Periodic survey questions loaded: {len(periodic_survey_questions)}")
        
        if survey_data and survey_data.get('periodic_surveys'):
            logger.info(f"  - Processing {len(survey_data.get('periodic_surveys', []))} periodic surveys...")
            
            periodic_surveys_data = [
                {
                    'occurrence': survey.get('occurrence', idx + 1),
                    'timestamp': survey.get('timestamp'),
                    'status': survey.get('status', 'completed'),
                    'triggered_at_analysis_count': survey.get('analysisCount'),
                    'is_final': survey.get('is_final', False),
                    'triggered_at_submission': survey.get('triggered_at_submission', False),
                    
                    # STORE THE QUESTIONS that were asked
                    'questions': periodic_survey_questions,
                    
                    # Store responses with context
                    'responses': [
                        {
                            'question': resp.get('question'),
                            'response': resp.get('response')
                        } for resp in survey.get('responses', [])
                    ],
                    
                    # AI history captured when this survey was shown
                    'ai_history_at_survey_time': survey.get('ai_history_at_survey', {})
                }
                for idx, survey in enumerate(survey_data.get('periodic_surveys', []))
            ]
        logs_data = {
            'ai_guesses': {
                str(i): {
                    'ai_analysis': entry.problem_analysis,
                    'timestamp': entry.timestamp,
                    'next_line_suggestion': entry.next_line_suggestion,
                    'full_code_suggestion': entry.full_code_suggestion,
                    'user_code_snapshot': entry.user_code_snapshot
                } for i, entry in enumerate(session_log.ai_guess_history, 1)
            }
        }
        
        if session_log.condition == 1 and session_log.correct_description_history:
            logs_data['correct_guesses'] = {
                str(i): {
                    'correct_analysis': entry.problem_analysis,
                    'timestamp': entry.timestamp,
                    'next_line_suggestion': entry.next_line_suggestion,
                    'full_code_suggestion': entry.full_code_suggestion,
                    'user_code_snapshot': entry.user_code_snapshot
                } for i, entry in enumerate(session_log.correct_description_history, 1)
            }
        elif session_log.condition == 3 and session_log.suggestions_only_history:
            logs_data['suggestions_only'] = {
                str(i): {
                    'suggestion_only': entry.next_line_suggestion,
                    'timestamp': entry.timestamp,
                    'full_code_suggestion': entry.full_code_suggestion,
                    'user_code_snapshot': entry.user_code_snapshot
                } for i, entry in enumerate(session_log.suggestions_only_history, 1)
            }
        activity_log_data = {
            'entries': [entry.dict() for entry in session_log.activity_log],
            'summary': session_log.activity_summary.dict()
        }
        user_data = {
            'session_metadata': session_metadata,
            'logs': logs_data,
            'activity_log': activity_log_data
        }
        
        # Add surveys as separate top-level entries
        if pre_survey:
            user_data['pre_survey'] = pre_survey
        
        if post_survey:
            user_data['post_survey'] = post_survey
        
        if periodic_surveys_data:
            survey_meta = survey_data or {}
            user_data['periodic_surveys'] = {
                'metadata': {
                    'total_occurrences': survey_meta.get('total_occurrences', len(periodic_surveys_data)),
                    'survey_interval': survey_meta.get('survey_interval', 4),
                    'questions_asked': periodic_survey_questions  # Store questions for reference
                },
                'occurrences': {
                    f"occurrence_{survey['occurrence']}": {
                        'occurrence_number': survey['occurrence'],
                        'timestamp': survey['timestamp'],
                        'triggered_at_analysis_count': survey['triggered_at_analysis_count'],
                        'is_final': survey['is_final'],
                        'triggered_at_submission': survey.get('triggered_at_submission', False),
                        'questions': survey['questions'],
                        'responses': survey['responses'],
                        'ai_history_at_survey_time': survey['ai_history_at_survey_time']
                    } for survey in periodic_surveys_data
                }
            }
        user_ref = firebase_db.child(f'{condition_path}/{session_log.assignment_id}/{session_log.user_id}')
        if not firebase_rtdb_set_with_retry(user_ref, user_data, max_attempts=3):
            return False

        # Mirror final payload under sessions/{session_id} for recovery / cross-refs
        session_ref = firebase_db.child("sessions").child(session_log.session_id).child("final_submit")
        firebase_rtdb_set_with_retry(
            session_ref,
            {
                "checkpoint_type": "final_submit",
                "checkpoint_time_utc": datetime.now(timezone.utc).isoformat(),
                "condition_path": condition_path,
                "user_data": user_data,
            },
            max_attempts=3,
        )

        logger.info(f"Session with separated surveys saved to Firebase: {condition_path}/{session_log.assignment_id}/{session_log.user_id}")
        logger.info(f"  - Session metadata: {len(str(session_metadata))} bytes")
        logger.info(f"  - Pre-survey: {'saved' if pre_survey else 'not present'}")
        logger.info(f"  - Post-survey: {'saved' if post_survey else 'not present'}")
        
        if periodic_surveys_data:
            logger.info(f"  - Periodic surveys: {len(periodic_surveys_data)} occurrences")
            for survey in periodic_surveys_data:
                occurrence_type = "FINAL" if survey['is_final'] else f"#{survey['occurrence']}"
                logger.info(f"    • Occurrence {occurrence_type}: {survey['triggered_at_analysis_count']} analyses, {len(survey['responses'])} responses")
        else:
            logger.info(f"  - Periodic surveys: none")
            
        logger.info(f"  - AI logs: {len(logs_data.get('ai_guesses', {}))} AI analyses")
        logger.info(f"  - Activity log: {len(session_log.activity_log)} entries")
        
        return True
        
    except Exception as e:
        logger.error(f"Firebase save failed: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False
    

@app.get("/survey.html", response_class=HTMLResponse)
async def serve_survey(request: Request):
    """Serve the survey page"""
    survey_type = request.query_params.get("type")
    
    if not survey_type or survey_type not in ['pre', 'post']:
        logger.warning(f"Invalid survey type accessed: {survey_type}")
        return RedirectResponse(url="/home.html", status_code=302)
    
    return templates.TemplateResponse("survey.html", {
        "request": request,
        "survey_type": survey_type
    })


@app.get("/admin-panel", response_class=HTMLResponse)
@app.get("/admin-panel.html", response_class=HTMLResponse)
async def serve_admin_panel(request: Request):
    """Serve the admin panel page"""
    return templates.TemplateResponse("admin_client.html", {"request": request})

@app.get("/download-assignment/{assignment_key}")
async def download_assignment(assignment_key: str):
    """Download assignment PDF based on assignment key - CASE-INSENSITIVE"""
    try:
        # Get base key for file reading
        base_key = get_assignment_base_key(assignment_key)
        if not base_key:
            raise HTTPException(status_code=400, detail="Invalid assignment key")
        
        # Case-insensitive PDF file search
        assignment_dir = Path("assignment")
        pdf_file = None
        
        if assignment_dir.exists():
            base_key_lower = base_key.lower()
            try:
                for file in assignment_dir.iterdir():
                    if file.is_file() and file.stem.lower() == base_key_lower and file.suffix == '.pdf':
                        pdf_file = file
                        logger.info(f"Found assignment PDF: {file}")
                        break
            except Exception as e:
                logger.error(f"Error searching for assignment PDF: {e}")
        
        if not pdf_file or not pdf_file.exists():
            logger.warning(f"Assignment PDF not found for: {base_key}")
            raise HTTPException(status_code=404, detail="Assignment PDF not found")
        
        logger.info(f"Downloading assignment PDF: {pdf_file}")
        
        return FileResponse(
            path=pdf_file,
            media_type='application/pdf',
            filename=f"{base_key}.pdf",
            headers={
                "Content-Disposition": f"attachment; filename={base_key}.pdf"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading assignment PDF: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to download assignment: {str(e)}")


@app.get("/assignment-text/{assignment_key}")
async def get_assignment_text(assignment_key: str, problem_index: int = 0):
    """Return the assignment .txt file content for the left problem panel."""
    try:
        # Instructor-created quiz: load problem statement from Firebase
        if assignment_key.startswith("quiz_") and firebase_db:
            access_code = assignment_key[5:]  # strip "quiz_"
            # Look up access code to get actual quiz UUID
            access_ref = fb_dict(db.reference(f"quiz_access/{access_code}").get())
            if access_ref and access_ref.get("quiz_id"):
                qval = fb_dict(db.reference(f"quizzes/{access_ref['quiz_id']}").get())
                if qval:
                    qval = normalize_quiz_data(qval)
                    import html as html_mod
                    parts = []
                    # No quiz-level title/description; teaser is shown in overlay at start
                    # Load problem data from the problems array
                    problems = qval.get("problems", [])
                    problem = problems[problem_index] if problem_index < len(problems) else (problems[0] if problems else {})
                    # Per-problem title (shown above problem statement)
                    prob_title = (problem.get("title") or "").strip()
                    if prob_title:
                        parts.append(f'<div class="problem-title">{html_mod.escape(prob_title)}</div>')
                    if problem.get("problem_statement"):
                        body, is_rich = format_instructor_problem_html_field(problem["problem_statement"])
                        if body:
                            cls = "section-content section-content--rich" if is_rich else "section-content"
                            parts.append(
                                '<div class="problem-section">'
                                '<span class="section-label">Problem Description</span>'
                                f'<div class="{cls}">{body}</div>'
                                "</div>"
                            )
                    if problem.get("test_cases"):
                        body, is_rich = format_instructor_problem_html_field(problem["test_cases"])
                        if body:
                            cls = "section-content section-content--mono"
                            if is_rich:
                                cls += " section-content--rich"
                            parts.append(
                                '<div class="problem-section">'
                                '<span class="section-label">Test Cases</span>'
                                f'<div class="{cls}">{body}</div>'
                                "</div>"
                            )
                    if problem.get("constraints"):
                        body, is_rich = format_instructor_problem_html_field(problem["constraints"])
                        if body:
                            cls = "section-content section-content--rich" if is_rich else "section-content"
                            parts.append(
                                '<div class="problem-section">'
                                '<span class="section-label">Constraints</span>'
                                f'<div class="{cls}">{body}</div>'
                                "</div>"
                            )
                    html_out = "".join(parts)
                    return {"html": html_out if html_out else None, "text": "No problem description provided." if not html_out else None}
            raise HTTPException(status_code=404, detail="Quiz not found")

        assignment_file = get_assignment_file_path(assignment_key)
        if not assignment_file or not assignment_file.exists():
            raise HTTPException(status_code=404, detail="Assignment text not found")
        with open(assignment_file, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        return {"text": content}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading assignment text: {e}")
        raise HTTPException(status_code=500, detail="Failed to read assignment text")


@app.get("/survey-images/{image_filename}")
async def get_survey_image(image_filename: str):
    """
    Serve survey images from the surveys/images directory
    Security: Only serve images from surveys/images folder
    """
    try:
        # Security: Prevent directory traversal attacks
        if '..' in image_filename or '/' in image_filename or '\\' in image_filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        
        # Whitelist allowed extensions
        allowed_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp'}
        file_ext = Path(image_filename).suffix.lower()
        
        if file_ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail="File type not allowed")
        
        # Construct safe path
        image_path = Path("surveys") / "images" / image_filename
        
        # Check if file exists
        if not image_path.exists():
            logger.warning(f"Survey image not found: {image_filename}")
            raise HTTPException(status_code=404, detail="Image not found")
        
        # Check if it's actually a file (not a directory)
        if not image_path.is_file():
            raise HTTPException(status_code=400, detail="Invalid resource")
        
        # Get MIME type
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(image_path))
        if not mime_type:
            mime_type = 'application/octet-stream'
        
        logger.info(f"Serving survey image: {image_filename}")
        
        return FileResponse(
            path=image_path,
            media_type=mime_type,
            filename=image_filename,
            headers={
                "Cache-Control": "public, max-age=3600",  # Cache for 1 hour
                "Content-Disposition": f"inline; filename={image_filename}"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving survey image {image_filename}: {e}")
        raise HTTPException(status_code=500, detail="Failed to serve image")


@app.get("/quiz-images/{asset_id}/{filename}")
async def get_quiz_image(asset_id: str, filename: str):
    """
    Serve per-quiz survey images uploaded by instructors (surveys/quiz_assets/{asset_id}/{filename}).
    asset_id is the quiz UUID (or draft upload id before first save).
    """
    try:
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        if not re.match(r"^[a-zA-Z0-9_-]{8,128}$", asset_id):
            raise HTTPException(status_code=400, detail="Invalid asset id")
        allowed_extensions = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}
        file_ext = Path(filename).suffix.lower()
        if file_ext not in allowed_extensions:
            raise HTTPException(status_code=400, detail="File type not allowed")
        base = Path("surveys") / "quiz_assets" / asset_id
        image_path = (base / filename).resolve()
        base_resolved = base.resolve()
        if not str(image_path).startswith(str(base_resolved)) or not image_path.is_file():
            raise HTTPException(status_code=404, detail="Image not found")
        mime_type, _ = mimetypes.guess_type(str(image_path))
        if not mime_type:
            mime_type = "application/octet-stream"
        return FileResponse(
            path=image_path,
            media_type=mime_type,
            filename=filename,
            headers={"Cache-Control": "public, max-age=3600", "Content-Disposition": f"inline; filename={filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error serving quiz image %s/%s: %s", asset_id, filename, e)
        raise HTTPException(status_code=500, detail="Failed to serve image")


@app.get("/validate-survey-images/{assignment_key}")
async def validate_survey_images(assignment_key: str):
    """
    Validate that all images referenced in survey config exist
    Useful for testing and debugging
    """
    try:
        base_key = get_assignment_base_key(assignment_key)
        if not base_key:
            raise HTTPException(status_code=400, detail="Invalid assignment key")
        
        survey_file = Path("surveys") / f"{base_key}.json"
        
        if not survey_file.exists():
            raise HTTPException(status_code=404, detail="Survey config not found")
        
        with open(survey_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        missing_images = []
        found_images = []
        
        # Check all survey sections
        for survey_type in ['pre_survey', 'post_survey']:
            if survey_type in config:
                survey = config[survey_type]
                
                # Check header image
                if 'header_image' in survey and 'src' in survey['header_image']:
                    img_src = survey['header_image']['src']
                    img_path = Path("surveys") / "images" / img_src
                    if img_path.exists():
                        found_images.append(img_src)
                    else:
                        missing_images.append(img_src)
                
                # Check question images
                for question in survey.get('questions', []):
                    # Single image
                    if 'image' in question and 'src' in question['image']:
                        img_src = question['image']['src']
                        img_path = Path("surveys") / "images" / img_src
                        if img_path.exists():
                            found_images.append(img_src)
                        else:
                            missing_images.append(img_src)
                    
                    # Multiple images
                    if 'images' in question:
                        for img in question['images']:
                            if 'src' in img:
                                img_src = img['src']
                                img_path = Path("surveys") / "images" / img_src
                                if img_path.exists():
                                    found_images.append(img_src)
                                else:
                                    missing_images.append(img_src)
        
        return {
            "assignment_key": assignment_key,
            "total_images": len(found_images) + len(missing_images),
            "found": len(found_images),
            "missing": len(missing_images),
            "found_images": found_images,
            "missing_images": missing_images,
            "status": "valid" if len(missing_images) == 0 else "invalid"
        }
        
    except Exception as e:
        logger.error(f"Image validation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
# Add to main.py after other @app.get endpoints

@app.get("/debug/sidebar-config")
async def debug_sidebar_config():
    """Debug sidebar configuration"""
    processed = get_side_panel_hidden_assignment_keys()
    return {
        "SIDE_PANEL_HIDDEN_raw": os.getenv("SIDE_PANEL_HIDDEN", "NOT_SET"),
        "processed_prefixes": processed,
        "split_prefixes": [p for p in processed.split(",") if p] if processed else [],
        "environment_loaded": bool(os.getenv("OPENAI_API_KEY")),
    }
    
# ─── Quiz data helpers ──────────────────────────────────────────────────────

def normalize_quiz_data(quiz_data: dict) -> dict:
    """Backward-compat: wrap legacy single-problem/single-condition into list format."""
    if "problems" not in quiz_data and "problem_statement" in quiz_data:
        quiz_data["problems"] = [{
            "problem_statement": quiz_data.pop("problem_statement", ""),
            "constraints": quiz_data.pop("constraints", ""),
            "starter_code": quiz_data.pop("starter_code", {}),
        }]
    if "conditions" not in quiz_data and "condition" in quiz_data:
        quiz_data["conditions"] = [int(quiz_data.pop("condition", 1))]
    elif "conditions" not in quiz_data:
        quiz_data["conditions"] = [1]

    # Migrate quiz-level surveys into per-problem surveys if missing
    if "problems" in quiz_data:
        quiz_pre = quiz_data.get("pre_survey", [])
        quiz_post = quiz_data.get("post_survey", [])
        quiz_rec = quiz_data.get("recurrent_survey", [])
        quiz_rec_interval = quiz_data.get("recurrent_survey_interval", 4)
        quiz_rec_start = quiz_data.get("recurrent_survey_start", 5)
        for prob in quiz_data["problems"]:
            if not prob.get("_has_surveys"):
                prob["_has_surveys"] = True
                prob["pre_survey_enabled"] = bool(quiz_pre)
                prob["pre_survey"] = quiz_pre
                prob["post_survey_enabled"] = bool(quiz_post)
                prob["post_survey"] = quiz_post
                prob["recurrent_survey_enabled"] = bool(quiz_rec)
                prob["recurrent_survey"] = quiz_rec
                prob["recurrent_survey_interval"] = quiz_rec_interval
                prob["recurrent_survey_start"] = quiz_rec_start

    return quiz_data

# ─── Student custom-quiz entry (via instructor share link/code) ─────────────

@app.get("/quiz/{access_code}", response_class=HTMLResponse)
async def student_quiz_entry(request: Request, access_code: str):
    """Landing page for students accessing an instructor-created quiz via share link."""
    quiz_data = None
    if firebase_db:
        mapping = fb_dict(db.reference(f"quiz_access/{access_code}").get())
        if mapping:
            quiz_id = mapping.get("quiz_id")
            quiz_data = fb_dict(db.reference(f"quizzes/{quiz_id}").get()) if quiz_id else None

    if not quiz_data:
        return HTMLResponse("<h2>Quiz not found or link is invalid.</h2>", status_code=404)

    if not quiz_data.get("published", False):
        return HTMLResponse("<h2>This quiz is not currently available.</h2>", status_code=403)

    # Check availability window
    now = datetime.utcnow()
    avail_start = quiz_data.get("availability_start")
    avail_end = quiz_data.get("availability_end")
    if avail_start:
        try:
            if now < datetime.fromisoformat(avail_start):
                return HTMLResponse("<h2>This quiz has not started yet.</h2>", status_code=403)
        except ValueError:
            pass
    if avail_end:
        try:
            if now > datetime.fromisoformat(avail_end):
                return HTMLResponse("<h2>This quiz is no longer available.</h2>", status_code=403)
        except ValueError:
            pass

    allowed_langs = quiz_data.get("allowed_languages", ["python", "javascript", "c"])
    quiz_teaser = quiz_data.get("teaser", "")
    return templates.TemplateResponse("home.html", {
        "request": request,
        "quiz_mode": True,
        "quiz_teaser": quiz_teaser,
        "access_code": access_code,
        "allowed_languages": allowed_langs,
    })


def build_quiz_join_response(
    session_log: SessionLog,
    quiz_data: Dict[str, Any],
    programming_language: str,
    problem_idx: int,
    message: str,
) -> Dict[str, Any]:
    """Shared JSON shape for /quiz/join and resume."""
    problems = quiz_data.get("problems", []) or []
    prob = problems[problem_idx] if problem_idx < len(problems) else {}
    problem_description = prob.get("problem_statement", "")
    quiz_starter = prob.get("starter_code", {})
    lang_starter = quiz_starter.get(programming_language) if quiz_starter else None

    starter_files = None
    starter_code = None
    if isinstance(lang_starter, list) and lang_starter:
        starter_files = lang_starter
        starter_code = lang_starter[0].get("content", "") if lang_starter else ""
    elif isinstance(lang_starter, str) and lang_starter:
        starter_code = lang_starter
    else:
        starter_code = get_starter_code(quiz_data.get("title", ""), programming_language)

    has_pre = prob.get("pre_survey_enabled", False) and bool(prob.get("pre_survey"))
    has_post = prob.get("post_survey_enabled", False) and bool(prob.get("post_survey"))
    periodic_enabled = prob.get("recurrent_survey_enabled", False) and bool(prob.get("recurrent_survey"))

    return {
        "status": "success",
        "message": message,
        "condition": session_log.condition,
        "session_id": session_log.session_id,
        "problem_description": problem_description if session_log.condition == 1 else None,
        "programming_language": programming_language,
        "starter_code": starter_code,
        "starter_files": starter_files,
        "paste_allowed": 1 if quiz_data.get("paste_allowed", True) else 0,
        "tab_tracking_enabled": quiz_data.get("tab_tracking_enabled", True),
        "has_pre_survey": has_pre,
        "has_post_survey": has_post,
        "periodic_survey_enabled": periodic_enabled,
        "periodic_survey_start": int(prob.get("recurrent_survey_start", 5)),
        "periodic_survey_interval": int(prob.get("recurrent_survey_interval", 4)),
        "final_survey_disabled": False,
        "quiz_id": session_log.quiz_id,
        "quiz_teaser": quiz_data.get("teaser", ""),
        "time_limit_minutes": quiz_data.get("time_limit_minutes", 0),
        "total_problems": len(problems),
        "current_problem_index": problem_idx,
    }


def try_resume_quiz_session(
    user_id: str,
    quiz_id: str,
    quiz_data: Dict[str, Any],
    programming_language: str,
) -> Optional[Dict[str, Any]]:
    """Restore in-progress quiz from Firebase pointer or latest disk session log."""
    problems = quiz_data.get("problems", []) or []
    if not problems:
        return None

    sl: Optional[SessionLog] = None
    if firebase_db:
        ref = quiz_resume_ref(quiz_id, user_id)
        if ref:
            rdata = ref.get()
            if isinstance(rdata, dict) and not rdata.get("completed"):
                sid = (rdata.get("session_id") or "").strip()
                if sid:
                    sl = load_session_log_from_disk(sid)

    if sl is None:
        p = find_latest_quiz_session_log_path(user_id, quiz_id)
        if p:
            sl = load_session_log_from_disk(p.stem)

    if sl is None:
        return None
    if sl.quiz_id != quiz_id or sl.user_id != user_id:
        return None
    if sl.total_problems != len(problems):
        logger.info("Quiz problem count changed; skipping resume for %s", user_id)
        return None

    idx = int(sl.current_problem_index or 0)
    if idx >= len(problems):
        return None

    idx = max(0, min(idx, len(problems) - 1))
    sl.current_problem_index = idx
    prob = problems[idx]
    if sl.condition == 1:
        sl.problem_description = prob.get("problem_statement", "")

    active_sessions[sl.session_id] = sl
    save_session_to_local(sl)
    quiz_resume_write(quiz_id, user_id, sl.session_id, idx, len(problems), completed=False)

    return build_quiz_join_response(
        sl, quiz_data, programming_language, idx, "Resumed quiz session"
    )


@app.post("/quiz/join")
async def student_quiz_join(request: Request):
    """Authenticate a student into an instructor-created quiz."""
    body = await request.json()
    access_code = (body.get("access_code") or "").strip()
    user_id = (body.get("user_id") or "").strip()
    programming_language = (body.get("programming_language") or "python").strip()

    if len(user_id) < 3:
        raise HTTPException(status_code=400, detail="User ID must be at least 3 characters")

    if not firebase_db:
        raise HTTPException(status_code=503, detail="Database unavailable")

    mapping = fb_dict(db.reference(f"quiz_access/{access_code}").get())
    if not mapping:
        raise HTTPException(status_code=404, detail="Invalid quiz code")

    quiz_id = mapping.get("quiz_id")
    if not quiz_id:
        raise HTTPException(status_code=404, detail="Invalid quiz code")

    quiz_data = fb_dict(db.reference(f"quizzes/{quiz_id}").get())
    if not quiz_data:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if not quiz_data.get("published", False):
        raise HTTPException(status_code=403, detail="Quiz not available")

    # Check availability window
    now = datetime.utcnow()
    avail_start = quiz_data.get("availability_start")
    avail_end = quiz_data.get("availability_end")
    if avail_start:
        try:
            if now < datetime.fromisoformat(avail_start):
                raise HTTPException(status_code=403, detail="Quiz has not started yet")
        except ValueError:
            pass
    if avail_end:
        try:
            if now > datetime.fromisoformat(avail_end):
                raise HTTPException(status_code=403, detail="Quiz is no longer available")
        except ValueError:
            pass

    # Validate language
    allowed_langs = quiz_data.get("allowed_languages", ["python", "javascript", "c"])
    if programming_language not in allowed_langs:
        raise HTTPException(status_code=400, detail=f"Language not allowed. Choose from: {allowed_langs}")

    # Normalize legacy quiz data to new format
    quiz_data = normalize_quiz_data(quiz_data)

    resumed = try_resume_quiz_session(user_id, quiz_id, quiz_data, programming_language)
    if resumed:
        return resumed

    import random

    conditions = quiz_data.get("conditions", [1])
    condition = random.choice(conditions)

    session_id = f"{user_id}_quiz_{quiz_id[:8]}_{uuid.uuid4().hex[:8]}"
    problems = quiz_data.get("problems", [])
    first_problem = problems[0] if problems else {}
    problem_description = first_problem.get("problem_statement", "")

    session_log = SessionLog(
        user_id=user_id,
        assignment_id=f"quiz_{quiz_id[:8]}",
        session_id=session_id,
        condition=condition,
        code_start_timestamp=datetime.now().isoformat(),
        current_problem_index=0,
        total_problems=len(problems),
        quiz_id=quiz_id,
        per_problem_submissions=[],
        ai_guess_history=[],
        activity_log=[],
        activity_summary=ActivitySummary(
            total_keystrokes=0, total_code_executions=0,
            total_suggestion_clicks=0, total_history_views=0,
            focus_time_seconds=0, away_time_seconds=0,
            font_changes=0, terminal_clears=0,
            surveys_completed=0, peak_code_lines=0,
            total_ai_analyses=0,
        ),
    )
    if condition == 1:
        session_log.problem_description = problem_description

    active_sessions[session_id] = session_log
    save_session_to_local(session_log)
    quiz_resume_write(quiz_id, user_id, session_id, 0, len(problems), completed=False)

    return build_quiz_join_response(
        session_log, quiz_data, programming_language, 0, "Joined quiz successfully"
    )


@app.post("/quiz/next-problem")
async def quiz_next_problem(request: Request):
    """Advance to the next problem in a multi-problem quiz."""
    body = await request.json()
    session_id = body.get("session_id", "").strip()
    code = body.get("code", "")
    programming_language = body.get("programming_language", "python").strip()

    if not session_id or session_id not in active_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session_log = active_sessions[session_id]

    # Save current problem's code
    session_log.per_problem_submissions.append({
        "problem_index": session_log.current_problem_index,
        "code": code,
        "timestamp": datetime.now().isoformat(),
    })

    # Advance to next problem
    session_log.current_problem_index += 1
    idx = session_log.current_problem_index

    if idx >= session_log.total_problems:
        if session_log.quiz_id:
            quiz_resume_write(
                session_log.quiz_id,
                session_log.user_id,
                session_id,
                session_log.total_problems,
                session_log.total_problems,
                completed=True,
            )
        return {"status": "complete", "message": "All problems completed"}

    # Reset recurrent survey counter for the new problem
    session_log.periodic_survey_count = 0

    # Reset survey statuses for the new problem
    session_log.pre_survey_status = "not_started"
    session_log.post_survey_status = "not_started"
    session_log.post_survey_timestamp = None

    # Load next problem from Firebase
    if not session_log.quiz_id:
        raise HTTPException(status_code=400, detail="No quiz ID in session")

    quiz_data = fb_dict(db.reference(f"quizzes/{session_log.quiz_id}").get())
    if not quiz_data:
        raise HTTPException(status_code=404, detail="Quiz not found")
    quiz_data = normalize_quiz_data(quiz_data)

    problems = quiz_data.get("problems", [])
    if idx >= len(problems):
        if session_log.quiz_id:
            quiz_resume_write(
                session_log.quiz_id,
                session_log.user_id,
                session_id,
                session_log.total_problems,
                session_log.total_problems,
                completed=True,
            )
        return {"status": "complete", "message": "All problems completed"}

    problem = problems[idx]
    problem_description = problem.get("problem_statement", "")
    completed_problem = problems[idx - 1] if idx > 0 else {}
    completed_problem_title = (completed_problem.get("title") or "").strip() or f"Problem {idx}"

    # Get starter code for the next problem
    quiz_starter = problem.get("starter_code", {})
    lang_starter = quiz_starter.get(programming_language) if quiz_starter else None

    starter_files = None
    starter_code = None
    if isinstance(lang_starter, list) and lang_starter:
        starter_files = lang_starter
        starter_code = lang_starter[0].get("content", "") if lang_starter else ""
    elif isinstance(lang_starter, str) and lang_starter:
        starter_code = lang_starter
    else:
        starter_code = ""

    # Update problem description in session if condition 1
    if session_log.condition == 1:
        session_log.problem_description = problem_description

    save_session_to_local(session_log)
    if session_log.quiz_id:
        quiz_resume_write(
            session_log.quiz_id,
            session_log.user_id,
            session_id,
            idx,
            session_log.total_problems,
            completed=False,
        )

    return {
        "status": "next",
        "problem_index": idx,
        "total_problems": session_log.total_problems,
        "completed_problem_title": completed_problem_title,
        "problem_description": problem_description if session_log.condition == 1 else None,
        "constraints": problem.get("constraints", ""),
        "starter_code": starter_code,
        "starter_files": starter_files,
        "has_pre_survey": problem.get("pre_survey_enabled", False) and bool(problem.get("pre_survey")),
        "has_post_survey": problem.get("post_survey_enabled", False) and bool(problem.get("post_survey")),
        "periodic_survey_enabled": problem.get("recurrent_survey_enabled", False) and bool(problem.get("recurrent_survey")),
        "periodic_survey_start": int(problem.get("recurrent_survey_start", 5)),
        "periodic_survey_interval": int(problem.get("recurrent_survey_interval", 4)),
    }


if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    reload = os.getenv("API_RELOAD", "True").lower() == "true"
    log_level = os.getenv("LOG_LEVEL", "info")
    
    logger.info("Starting AI Code Assistant with Firebase Integration...")
    logger.info(f"Frontend: http://localhost:{port}")
    logger.info(f"API Docs: http://localhost:{port}/docs")
    logger.info(f"Firebase Status: {'Connected' if firebase_db else 'Disconnected'}")
    
    if reload:
        # When reload is enabled, pass app as import string
        uvicorn.run(
            "main:app",  # Import string format: "module:variable"
            host=host, 
            port=port,
            log_level=log_level,
            reload=reload
        )
    else:
        # When reload is disabled, can pass app object directly
        uvicorn.run(
            app, 
            host=host, 
            port=port,
            log_level=log_level,
            reload=reload
        )