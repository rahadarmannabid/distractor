from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from openai import OpenAI
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import uuid
import json
from datetime import datetime
from typing import List, Dict, Optional
import firebase_admin
from firebase_admin import credentials, db
from fastapi.responses import HTMLResponse, RedirectResponse
import asyncio
import shlex
from typing import List, Dict, Optional, Any
import smtplib
import email
from email import encoders
import traceback
# Add these to your existing imports section
from typing import List, Dict, Optional, Any
from enum import Enum
import json
from pathlib import Path
from fastapi.responses import FileResponse
from admin import router as admin_router  # ADD THIS LINE



# Load environment variables
load_dotenv()

# Add logging and debugging capabilities
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Firebase
def initialize_firebase():
    """Initialize Firebase Admin SDK using environment variables"""
    try:
        if not firebase_admin._apps:
            # Try to initialize from individual environment variables first
            if all(os.getenv(var) for var in [
                "FIREBASE_PROJECT_ID", "FIREBASE_PRIVATE_KEY_ID", 
                "FIREBASE_PRIVATE_KEY", "FIREBASE_CLIENT_EMAIL", "FIREBASE_CLIENT_ID"
            ]):
                # Use individual environment variables (new method)
                cred_dict = {
                    "type": "service_account",
                    "project_id": os.getenv("FIREBASE_PROJECT_ID"),
                    "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
                    "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace("\\n", "\n"),
                    "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
                    "client_id": os.getenv("FIREBASE_CLIENT_ID"),
                    "auth_uri": os.getenv("FIREBASE_AUTH_URI", "https://accounts.google.com/o/oauth2/auth"),
                    "token_uri": os.getenv("FIREBASE_TOKEN_URI", "https://oauth2.googleapis.com/token"),
                    "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs"),
                    "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_CERT_URL"),
                    "universe_domain": os.getenv("FIREBASE_UNIVERSE_DOMAIN", "googleapis.com")
                }
                
                cred = credentials.Certificate(cred_dict)
                
                # Initialize with database URL for Realtime Database
                database_url = os.getenv("FIREBASE_DATABASE_URL")
                if database_url:
                    firebase_admin.initialize_app(cred, {
                        'databaseURL': database_url
                    })
                    logger.info("Firebase Realtime Database initialized successfully")
                    return db.reference()
                else:
                    logger.warning("FIREBASE_DATABASE_URL not found in environment variables")
                    return None
                
            # Fallback to service account file if available
            elif os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                if os.path.exists(cred_path):
                    cred = credentials.Certificate(cred_path)
                    database_url = os.getenv("FIREBASE_DATABASE_URL")
                    if database_url:
                        firebase_admin.initialize_app(cred, {
                            'databaseURL': database_url
                        })
                        logger.info("Firebase Realtime Database initialized successfully using service account file")
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

# Initialize Firebase client
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


def get_assignment_base_key(assignment_key):
    """
    Extract the base key from assignment key for file reading
    Returns the base prefix that matches the assignment key
    """
    if not assignment_key:
        return None
    
    condition_1_keys, condition_2_keys, condition_3_keys = get_condition_keys()
    all_keys = condition_1_keys + condition_2_keys + condition_3_keys
    assignment_key = assignment_key.strip()
    
    # Check for exact match first
    if assignment_key in all_keys:
        return assignment_key
    
    # Check for prefix match and return the matching base key
    for base_key in all_keys:
        if assignment_key.startswith(base_key):
            return base_key
    
    return None

def get_assignment_condition(assignment_key):
    """
    Determine which condition the assignment key belongs to
    Returns: 1 for full UI, 2 for partial UI (guess only), 3 for suggestions only, None for invalid
    """
    if not assignment_key:
        return None
    
    condition_1_keys, condition_2_keys, condition_3_keys = get_condition_keys()
    assignment_key = assignment_key.strip()
    
    # Check for exact match in condition 1 keys
    if assignment_key in condition_1_keys:
        return 1
    
    # Check for exact match in condition 2 keys
    if assignment_key in condition_2_keys:
        return 2
        
    # Check for exact match in condition 3 keys
    if assignment_key in condition_3_keys:
        return 3
    
    # Check for prefix match in condition 1
    for prefix in condition_1_keys:
        if assignment_key.startswith(prefix):
            return 1
    
    # Check for prefix match in condition 2
    for prefix in condition_2_keys:
        if assignment_key.startswith(prefix):
            return 2
            
    # Check for prefix match in condition 3
    for prefix in condition_3_keys:
        if assignment_key.startswith(prefix):
            return 3
    
    # STRICT VALIDATION: Return None for unrecognized keys
    return None

def get_assignment_file_path(assignment_key):
    """
    Get the correct assignment file path based on assignment key
    Returns Path object or None if not found
    """
    base_key = get_assignment_base_key(assignment_key)
    if not base_key:
        return None
    
    assignment_file = Path("assignment") / f"{base_key}.txt"
    return assignment_file if assignment_file.exists() else None


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
        
        # Create message using email.message
        msg = email.message.EmailMessage()
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

async def execute_code_safely(code: str, language: str, timeout: int = 10):
    """
    Execute code safely in a subprocess with timeout and security restrictions
    """
    if not code.strip():
        return {"success": False, "output": "", "error": "No code provided"}
    
    try:
        if language == "python":
            return await execute_python_code(code, timeout)
        elif language == "c":
            return await execute_c_code(code, timeout)
        elif language == "javascript":
            return {"success": False, "output": "", "error": "JavaScript should be executed in frontend"}
        else:
            return {"success": False, "output": "", "error": f"Language '{language}' not supported for backend execution"}
    except Exception as e:
        logger.error(f"Code execution error: {e}")
        return {"success": False, "output": "", "error": f"Execution failed: {str(e)}"}

async def execute_python_code(code: str, timeout: int = 10):
    """
    Execute Python code safely with restrictions
    """
    # Security restrictions - block dangerous imports and operations
    dangerous_patterns = [
        'import os', 'import sys', 'import subprocess', 'import shutil',
        'import socket', 'import urllib', 'import requests', 'import http',
        'open(', 'file(', '__import__', 'eval(', 'exec(',
        'compile(', 'globals()', 'locals()', 'vars()', 'dir(',
        'getattr(', 'setattr(', 'delattr(', 'hasattr(',
        'input(', 'raw_input('
    ]
    
    code_lower = code.lower()
    for pattern in dangerous_patterns:
        if pattern in code_lower:
            return {
                "success": False, 
                "output": "", 
                "error": f"Security restriction: '{pattern}' is not allowed for safety reasons"
            }
    
    # Create a temporary file
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as temp_file:
            temp_file.write(code)
            temp_file_path = temp_file.name
        
        # Execute with timeout and restricted environment
        process = await asyncio.create_subprocess_exec(
            sys.executable, temp_file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={'PYTHONPATH': '', 'PATH': '/usr/bin:/bin'}  # Restricted environment
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            
            stdout_str = stdout.decode('utf-8', errors='replace')
            stderr_str = stderr.decode('utf-8', errors='replace')
            
            # Clean up temp file
            try:
                os.unlink(temp_file_path)
            except:
                pass
            
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
            try:
                os.unlink(temp_file_path)
            except:
                pass
            return {
                "success": False,
                "output": "",
                "error": f"Code execution timed out after {timeout} seconds"
            }
            
    except Exception as e:
        try:
            os.unlink(temp_file_path)
        except:
            pass
        return {
            "success": False,
            "output": "",
            "error": f"Execution error: {str(e)}"
        }
async def execute_c_code(code: str, timeout: int = 10):
    """
    Execute C code safely with security restrictions and async compilation
    """
    # Security restrictions - block dangerous includes and functions
    dangerous_patterns = [
        # Dangerous system includes
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
        
        # Dangerous function calls
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
    
    code_lower = code.lower()
    for pattern in dangerous_patterns:
        if pattern.lower() in code_lower:
            return {
                "success": False,
                "output": "",
                "error": f"Security restriction: '{pattern}' is not allowed for safety reasons"
            }
    
    # Create temporary directory
    tmpdir = tempfile.mkdtemp()
    
    try:
        c_file_path = os.path.join(tmpdir, 'temp.c')
        exe_file_path = os.path.join(tmpdir, 'temp.out')

        # Write the C code to a file
        try:
            with open(c_file_path, 'w', encoding='utf-8') as c_file:
                c_file.write(code)
        except Exception as e:
            return {
                "success": False,
                "output": "",
                "error": f"Failed to write C file: {str(e)}"
            }

        # Compile the C code using ASYNC subprocess - OPTIMIZED FOR SPEED
        compile_process = await asyncio.create_subprocess_exec(
            'gcc', c_file_path, '-o', exe_file_path, '-std=c11',
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

class AuthResponse(BaseModel):
    status: str
    message: str
    condition: int
    problem_description: str | None = None
    session_id: str
    paste_allowed: int = 1
    has_pre_survey: bool = False   # ADD THIS
    has_post_survey: bool = False  # ADD THIS

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

class SessionLog(BaseModel):
    user_id: str
    assignment_id: str
    session_id: str
    condition: int
    code_start_timestamp: str
    code_submit_timestamp: Optional[str] = None
    submitted_full_code: Optional[str] = None
    
    # ADD THESE NEW FIELDS FOR SURVEY TRACKING
    pre_survey_status: str = "not_started"
    pre_survey_timestamp: Optional[str] = None
    pre_survey_responses: Optional[Dict[str, Any]] = None
    
    post_survey_status: str = "not_started"
    post_survey_timestamp: Optional[str] = None
    post_survey_responses: Optional[Dict[str, Any]] = None
    
    periodic_survey_count: int = 0
    
    # Your existing fields continue below...
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

app.include_router(admin_router)  # ADD THIS LINE



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

def extract_survey_ai_history_summary(session_log: SessionLog, survey_data: Dict[str, Any] = None) -> Dict[str, Any]:
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

def get_response(prompt):
    """Get response from OpenAI GPT model with error handling"""
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "system", "content": prompt}],
            model="gpt-4o-mini",
        )
        response = chat_completion.choices[0].message.content
        return response.strip() if response else "No response generated"
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return f"Error getting AI response: {str(e)}"

def analyze_problem_statement(existing_code):
    """Analyze what problem the programmer is trying to solve - language agnostic"""
    if not existing_code or not existing_code.strip():
        return "No code provided for analysis"
    
    language = detect_programming_language(existing_code)
    
    what_problem_is_this = f"""
    You are an expert code analyst. Analyze the following {language} code and provide a brief problem description:

    ```{language}
    {existing_code}
    ```

    Provide ONLY a brief description of what problem this code is trying to solve. Keep it concise.
    
    Examples of good responses:
    - "This code implements a factorial function using recursion"
    - "Building a sorting algorithm to arrange numbers in ascending order"
    - "Creating a class to represent a data structure with methods"
    - "Implementing a search function to find elements in a list"
    - "This code creates a simple calculator interface"
    - "Building a web form with input validation"
    
    Do NOT include sections, bullet points, or markdown formatting. Just a simple, clear description.
    """
    
    problem_description = get_response(what_problem_is_this)
    return problem_description

def predict_next_line(problem_description, existing_code):
    """Predict the next line of code based on problem and existing code - language agnostic"""
    
    if not existing_code or not existing_code.strip():
        return "// Start coding here"
    
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
        "    // Continue implementation",
        "    variable = value;",
        "function_name() {"
    ])
    
    what_is_the_next_line = f"""
    You are an expert programmer. Based on this problem: {problem_description}
    
    Current {language} code:
    ```{language}
    {existing_code}
    ```
    
    Generate ONLY the next single line of code that should logically follow. 
    
    Rules:
    - Provide EXACTLY ONE line of code (maximum 2 lines if absolutely necessary)
    - Maintain proper {language} syntax and indentation
    - No explanations, comments, or multiple code blocks
    - Focus on the immediate next logical step
    
    Examples of good responses for {language}:
    {chr(10).join([f'    - "{example}"' for example in examples[:4]])}    
    Next line:
    """
    
    next_line = get_response(what_is_the_next_line)
    return next_line

def generate_full_code_suggestion(problem_description, existing_code, is_correct_description=False):
    """Generate full code completion suggestion"""
    if not existing_code or not existing_code.strip():
        return "// No code provided for completion"
    
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
    - Don't include any comments or extraneous text.
    - Output should be in markdown text. 

    Complete code:
    """
    
    full_suggestion = get_response(prompt)
    
    # Clean the response
    full_suggestion = re.sub(r'```\w*\n?', '', full_suggestion)
    full_suggestion = re.sub(r'```\n?', '', full_suggestion)
    full_suggestion = full_suggestion.strip()
    
    return full_suggestion


async def get_ai_next_line_async(problem_description, code):
    """Async wrapper for AI next line prediction"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, predict_next_line, problem_description, code)

async def get_correct_next_line_async(correct_description, code):
    """Async wrapper for correct description next line prediction"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, predict_next_line, correct_description, code)

async def get_full_code_suggestion_async(problem_description, code, is_correct_description=False):
    """Async wrapper for full code suggestion"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, generate_full_code_suggestion, problem_description, code, is_correct_description)

class CodeRequest(BaseModel):
    code: str
    user_id: str = None
    assignment_key: str = None
    session_id: str = None

class CodeResponse(BaseModel):
    analysis: str
    next_line: str
    correct_next_line: Optional[str] = None


class ExecuteRequest(BaseModel):
    code: str
    language: str
    user_id: str = None
    session_id: str = None

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
                next_line="// Start coding here"
            )
        
        # Enhanced analysis for very short code snippets
        if len(code.strip()) < 10:
            language = detect_programming_language(code)
            return CodeResponse(
                analysis=f"Code snippet in progress: {code.strip()} (detected: {language})",
                next_line="// Continue your statement"
            )
        
        # Step 1: Analyze what problem the code is trying to solve
        problem_description = analyze_problem_statement(code)
        
        # Clean problem description
        problem_description = re.sub(r'^\*\*.*?\*\*:\s*', '', problem_description)
        problem_description = re.sub(r'^Problem.*?:\s*', '', problem_description, flags=re.IGNORECASE)
        problem_description = problem_description.strip()
        
        # Step 2 & 3: Concurrent API calls for better performance
        session_log = None
        if session_id and session_id in active_sessions:
            session_log = active_sessions[session_id]
        
        # Prepare concurrent tasks
        tasks = []
        
        # Always get AI next line and full suggestion
        ai_next_line_task = get_ai_next_line_async(problem_description, code)
        ai_full_code_task = get_full_code_suggestion_async(problem_description, code, is_correct_description=False)
        tasks.extend([ai_next_line_task, ai_full_code_task])
        
        # For condition 1, also get correct description suggestions concurrently
        correct_next_line_task = None
        correct_full_code_task = None
        if session_log and session_log.condition == 1 and session_log.problem_description:
            correct_next_line_task = get_correct_next_line_async(session_log.problem_description, code)
            correct_full_code_task = get_full_code_suggestion_async(session_log.problem_description, code, is_correct_description=True)
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
        
        # Ensure we have something useful
        if not ai_next_line or len(ai_next_line.strip()) < 2:
            language = detect_programming_language(code)
            if language == "python":
                ai_next_line = "# Continue your implementation here"
            elif language in ["javascript", "java", "cpp", "csharp"]:
                ai_next_line = "// Continue your implementation here"
            elif language == "html":
                ai_next_line = "<!-- Continue your markup here -->"
            elif language == "css":
                ai_next_line = "/* Continue your styles here */"
            else:
                ai_next_line = "// Continue your implementation here"
        
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
            
            # Ensure we have something useful
            if not correct_next_line or len(correct_next_line.strip()) < 2:
                correct_next_line = ai_next_line  # Fallback to AI suggestion
        
        # Log this analysis to session if session_id provided
        if session_log:
            # Create AI guess entry for problem guessing
            ai_guess_entry = AIGuessEntry(
                problem_analysis=problem_description,
                next_line_suggestion=ai_next_line,
                full_code_suggestion=ai_full_code_suggestion,
                timestamp=datetime.now().isoformat(),
                analysis_type="ai_problem_guess"
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
                    analysis_type="correct_description"
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
                    analysis_type="suggestions_only"
                )
                session_log.suggestions_only_history.append(suggestions_entry)
            
            # Update local backup only
            save_session_to_local(session_log)
        
        # FIXED: Return both suggestions
        return CodeResponse(
            analysis=problem_description,
            next_line=ai_next_line,
            correct_next_line=correct_next_line if session_log and session_log.condition == 1 else None
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
            next_line="// Analysis temporarily unavailable - continue coding"
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
        
        start_time = time.time()
        result = await execute_code_safely(code, language, timeout=10)
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
    user_id: str = None
    assignment_key: str = None
    session_id: str = None
    programming_language: str = None
    timestamp: str = None
    suggestion_clicked: int = 0
    compiler_clicked: int = 0
    survey_data: Optional[Dict[str, Any]] = None  # ADD THIS LINE

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
        
        # Get session log
        session_log = None
        if session_id and session_id in active_sessions:
            session_log = active_sessions[session_id]
            # Update session with submission data
            session_log.code_submit_timestamp = timestamp
            session_log.submitted_full_code = code
        
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
                del active_sessions[session_id]
                
            except Exception as firebase_error:
                logger.error(f"Firebase submission save failed: {firebase_error}")
                firebase_status = "failed"
                
                # Still remove from active sessions even if Firebase fails
                if session_id in active_sessions:
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
        "user_id": user_id
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
            "FIREBASE_PROJECT_ID": bool(os.getenv("FIREBASE_PROJECT_ID")),
            "FIREBASE_PRIVATE_KEY": bool(os.getenv("FIREBASE_PRIVATE_KEY")),
            "FIREBASE_DATABASE_URL": os.getenv("FIREBASE_DATABASE_URL"),
            "FIREBASE_CLIENT_EMAIL": os.getenv("FIREBASE_CLIENT_EMAIL"),
            "GOOGLE_APPLICATION_CREDENTIALS": bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
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
async def get_survey_config():
    """Get survey configuration from environment variables"""
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
            return {"status": "success", "logged_activities": 0, "message": "No activities to log"}
        
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
        
        return {"status": "success", "logged_activities": len(request.activities)}
        
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
        
        return {
            "status": "success",
            "message": f"{request.survey_type.title()} survey saved successfully",
            "timestamp": request.timestamp,
            "ai_history_entries": len(request.ai_history_context.get('ai_guess_history', []) if request.ai_history_context else 0)
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
    type: str  # likert, multiple_choice, open_ended
    question: str
    required: bool
    # Type-specific fields
    scale: Optional[Dict[str, Any]] = None  # For likert questions
    options: Optional[List[str]] = None      # For multiple choice
    placeholder: Optional[str] = None        # For open-ended
    max_length: Optional[int] = None         # For open-ended


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
async def get_survey_config(assignment_key: str):
    """
    Get pre, post, and periodic survey configuration for an assignment key
    """
    try:
        # Get base key for file reading
        base_key = get_assignment_base_key(assignment_key)
        if not base_key:
            raise HTTPException(status_code=400, detail="Invalid assignment key")
        
        # Try to load assignment-specific survey
        survey_file = Path("surveys") / f"{base_key}.json"
        
        if not survey_file.exists():
            # Fallback to default survey
            survey_file = Path("surveys") / "default_survey.json"
            logger.warning(f"Survey config not found for {base_key}, using default")
        
        if not survey_file.exists():
            # Return empty config if no default exists
            logger.error("No survey configuration found")
            return {
                "assignment_key": base_key,
                "pre_survey": {"title": "", "description": "", "questions": []},
                "post_survey": {"title": "", "description": "", "questions": []},
                "periodic_survey": {"enabled": False, "interval": 4, "questions": []}
            }
        
        with open(survey_file, 'r', encoding='utf-8') as f:
            survey_config = json.load(f)
        
        logger.info(f"Loaded survey config for {base_key}")
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
        
        if len(user_id) < 3:
            raise HTTPException(status_code=400, detail="User ID must be at least 3 characters")
        
        if len(assignment_key) < 5:
            raise HTTPException(status_code=400, detail="Assignment Key must be at least 5 characters")
        
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
        
        # ===== ADD THIS SECTION TO CHECK FOR SURVEYS =====
        base_key = get_assignment_base_key(assignment_key)
        survey_file = Path("surveys") / f"{base_key}.json"
        has_surveys = survey_file.exists()
        
        has_pre = False
        has_post = False
        if has_surveys:
            try:
                with open(survey_file, 'r', encoding='utf-8') as f:
                    survey_config = json.load(f)
                    has_pre = len(survey_config.get('pre_survey', {}).get('questions', [])) > 0
                    has_post = len(survey_config.get('post_survey', {}).get('questions', [])) > 0
            except Exception as e:
                logger.error(f"Error checking survey config: {e}")
        # ===== END OF NEW SECTION =====
        
        # Initialize session log
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
            
            # ===== ADD THESE SURVEY FIELDS =====
            pre_survey_status=SurveyStatus.NOT_REQUIRED if not has_pre else SurveyStatus.NOT_STARTED,
            post_survey_status=SurveyStatus.NOT_REQUIRED if not has_post else SurveyStatus.NOT_STARTED,
            # ===== END OF NEW FIELDS =====
        )
        
        active_sessions[session_id] = session_log
        save_session_to_local(session_log)
        
        logger.info(f"Authentication successful: {user_id} -> Condition {condition}, Session: {session_id}")
        
        paste_allowed = int(os.getenv("PASTE_CONTROL", "1"))
        
        return AuthResponse(
            status="success",
            message="Authentication successful",
            condition=condition,
            problem_description=problem_description,
            session_id=session_id,
            paste_allowed=paste_allowed,
            has_pre_survey=has_pre,   # ADD THIS
            has_post_survey=has_post  # ADD THIS
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Authentication failed: {str(e)}")


async def save_session_to_firebase(
    session_log: SessionLog, 
    suggestion_clicked: int, 
    compiler_clicked: int, 
    programming_language: str = "unknown", 
    survey_data: Dict[str, Any] = None
):
    """Save complete session data to Firebase with surveys as separate top-level entries"""
    if not firebase_db:
        logger.warning("Firebase not available for saving session data")
        return False
    
    try:
        condition_path = f"condition_{session_log.condition}"
        
        # ===== PREPARE SESSION METADATA =====
        session_metadata = {
            'user_id': session_log.user_id,
            'assignment_key': session_log.assignment_id,
            'session_id': session_log.session_id,
            'condition': session_log.condition,
            'programming_language': programming_language,
            'starting_time': session_log.code_start_timestamp,
            'end_time': session_log.code_submit_timestamp,
            'submitted_code': session_log.submitted_full_code,
            'suggestion_clicked': suggestion_clicked,
            'compiler_clicked': compiler_clicked,
        }
        
        # ===== PREPARE PRE-SURVEY =====
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
        
        # ===== PREPARE POST-SURVEY =====
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
        
        # ===== PREPARE PERIODIC SURVEYS WITH AI HISTORY =====
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
        
        # ===== PREPARE AI LOGS =====
        logs_data = {
            'ai_guesses': {
                str(i): {
                    'ai_analysis': entry.problem_analysis,
                    'timestamp': entry.timestamp,
                    'next_line_suggestion': entry.next_line_suggestion,
                    'full_code_suggestion': entry.full_code_suggestion
                } for i, entry in enumerate(session_log.ai_guess_history, 1)
            }
        }
        
        if session_log.condition == 1 and session_log.correct_description_history:
            logs_data['correct_guesses'] = {
                str(i): {
                    'correct_analysis': entry.problem_analysis,
                    'timestamp': entry.timestamp,
                    'next_line_suggestion': entry.next_line_suggestion,
                    'full_code_suggestion': entry.full_code_suggestion
                } for i, entry in enumerate(session_log.correct_description_history, 1)
            }
        elif session_log.condition == 3 and session_log.suggestions_only_history:
            logs_data['suggestions_only'] = {
                str(i): {
                    'suggestion_only': entry.next_line_suggestion,
                    'timestamp': entry.timestamp,
                    'full_code_suggestion': entry.full_code_suggestion
                } for i, entry in enumerate(session_log.suggestions_only_history, 1)
            }
        
        # ===== PREPARE ACTIVITY LOG =====
        activity_log_data = {
            'entries': [entry.dict() for entry in session_log.activity_log],
            'summary': session_log.activity_summary.dict()
        }
        
        # ===== BUILD FINAL USER DATA WITH SEPARATE SURVEYS =====
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
            user_data['periodic_surveys'] = {
                'metadata': {
                    'total_occurrences': survey_data.get('total_occurrences', len(periodic_surveys_data)),
                    'survey_interval': survey_data.get('survey_interval', 4),
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
        
        # ===== SAVE TO FIREBASE =====
        user_ref = firebase_db.child(f'{condition_path}/{session_log.assignment_id}/{session_log.user_id}')
        user_ref.set(user_data)
        
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
    """Download assignment PDF based on assignment key"""
    try:
        # Get base key for file reading
        base_key = get_assignment_base_key(assignment_key)
        if not base_key:
            raise HTTPException(status_code=400, detail="Invalid assignment key")
        
        # Look for PDF file
        pdf_file = Path("assignment") / f"{base_key}.pdf"
        
        if not pdf_file.exists():
            logger.warning(f"Assignment PDF not found: {pdf_file}")
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
    
    uvicorn.run(
        app, 
        host=host, 
        port=port,
        log_level=log_level,
        reload=reload
    )