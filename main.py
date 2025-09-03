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

# Assignment Key Conditions - Load from environment variables
def get_condition_keys():
    """Get condition keys from environment variables"""
    condition_1_keys = os.getenv("CONDITION_1_PREFIXES", "Condition1_all").split(",")
    condition_2_keys = os.getenv("CONDITION_2_PREFIXES", "Condition2_partial,fdasdfsa").split(",")
    
    # Clean up the keys (remove spaces and empty strings)
    condition_1_keys = [key.strip() for key in condition_1_keys if key.strip()]
    condition_2_keys = [key.strip() for key in condition_2_keys if key.strip()]
    
    return condition_1_keys, condition_2_keys

def get_assignment_base_key(assignment_key):
    """
    Extract the base key from assignment key for file reading
    Returns the base prefix that matches the assignment key
    """
    if not assignment_key:
        return None
    
    condition_1_keys, condition_2_keys = get_condition_keys()
    all_keys = condition_1_keys + condition_2_keys
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
    Returns: 1 for full UI, 2 for partial UI (guess only), None for invalid
    """
    if not assignment_key:
        return None
    
    condition_1_keys, condition_2_keys = get_condition_keys()
    assignment_key = assignment_key.strip()
    
    # Check for exact match in condition 1 keys
    if assignment_key in condition_1_keys:
        return 1
    
    # Check for exact match in condition 2 keys
    if assignment_key in condition_2_keys:
        return 2
    
    # Check for prefix match in condition 1
    for prefix in condition_1_keys:
        if assignment_key.startswith(prefix):
            return 1
    
    # Check for prefix match in condition 2
    for prefix in condition_2_keys:
        if assignment_key.startswith(prefix):
            return 2
    
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
    ai_guess_history: List[AIGuessEntry] = []
    correct_description_history: Optional[List[AIGuessEntry]] = None  # Only for condition 1
    problem_description: Optional[str] = None

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
        
        # Determine condition based on assignment key
        condition = get_assignment_condition(assignment_key)
        
        # STRICT VALIDATION: Reject if no matching condition
        if condition is None:
            raise HTTPException(status_code=400, detail="Invalid assignment key. Please check your credentials and try again.")
        
        # Generate new session ID
        session_id = f"{user_id}_{assignment_key}_{uuid.uuid4().hex[:8]}"
        
        # Get problem description if condition 1
        problem_description = None
        if condition == 1:
            try:
                # FIXED: Use base key for file reading
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
        
        # Initialize session log
        session_log = SessionLog(
            user_id=user_id,
            assignment_id=assignment_key,
            session_id=session_id,
            condition=condition,
            code_start_timestamp=datetime.now().isoformat(),
            ai_guess_history=[],
            correct_description_history=[] if condition == 1 else None,
            problem_description=problem_description
        )
        
        # Store session in memory
        active_sessions[session_id] = session_log
        
        # Save session log to local file for backup
        save_session_to_local(session_log)
        
        logger.info(f"Authentication successful: {user_id} -> Condition {condition}, Session: {session_id}")
        
        return AuthResponse(
            status="success",
            message="Authentication successful",
            condition=condition,
            problem_description=problem_description,
            session_id=session_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Authentication failed: {str(e)}")

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

async def save_session_to_firebase(session_log: SessionLog, suggestion_clicked: int, compiler_clicked: int):
    """Save complete session data to Firebase using the new hierarchical structure"""
    if not firebase_db:
        logger.warning("Firebase not available for saving session data")
        return False
    
    try:
        # Determine the condition path
        condition_path = f"condition_{session_log.condition}"
        
        # Create summary data
        summary_data = {
            'starting_time': session_log.code_start_timestamp,
            'submitted_code': session_log.submitted_full_code,
            'suggestion_clicked': suggestion_clicked,
            'compiler_clicked': compiler_clicked,
            'end_time': session_log.code_submit_timestamp
        }
        
        # Create AI guesses log data - FIXED field mapping
        ai_guesses = {}
        for i, entry in enumerate(session_log.ai_guess_history, 1):
            ai_guesses[str(i)] = {
                'ai_analysis': entry.problem_analysis,  # FIXED: Use correct field name
                'timestamp': entry.timestamp,
                'next_line_suggestion': entry.next_line_suggestion,
                'full_code_suggestion': entry.full_code_suggestion
            }
        
        # Create logs structure
        logs_data = {
            'ai_guesses': ai_guesses
        }
        
        # For condition 1, add correct guesses
        if session_log.condition == 1 and session_log.correct_description_history:
            correct_guesses = {}
            for i, entry in enumerate(session_log.correct_description_history, 1):
                correct_guesses[str(i)] = {
                    'correct_analysis': entry.problem_analysis,  # FIXED: Use correct field name
                    'timestamp': entry.timestamp,
                    'next_line_suggestion': entry.next_line_suggestion,
                    'full_code_suggestion': entry.full_code_suggestion
                }
            logs_data['correct_guesses'] = correct_guesses
        
        # Prepare the complete user data
        user_data = {
            'summary': summary_data,
            'logs': logs_data
        }
        
        # Save to Firebase using the hierarchical structure
        # Path: condition_1/assignment_key/user_id
        user_ref = firebase_db.child(f'{condition_path}/{session_log.assignment_id}/{session_log.user_id}')
        user_ref.set(user_data)
        
        logger.info(f"Session data saved to Firebase: {condition_path}/{session_log.assignment_id}/{session_log.user_id}")
        return True
        
    except Exception as e:
        logger.error(f"Firebase save failed: {e}")
        return False

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

class CodeRequest(BaseModel):
    code: str
    user_id: str = None
    assignment_key: str = None
    session_id: str = None

class CodeResponse(BaseModel):
    analysis: str
    next_line: str
    correct_next_line: Optional[str] = None

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
        
        # Step 2: Predict the next line based on AI analysis
        ai_next_line = predict_next_line(problem_description, code)
        
        # Enhanced cleaning for single line output
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
        
        # FIXED: Step 3 - For condition 1, generate separate next line based on correct description
        correct_next_line = ai_next_line  # Default fallback
        session_log = None
        if session_id and session_id in active_sessions:
            session_log = active_sessions[session_id]
            if session_log.condition == 1 and session_log.problem_description:
                # Generate next line based on correct problem description
                correct_next_line = predict_next_line(session_log.problem_description, code)
                
                # Clean the correct suggestion too
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
        
        # Step 4: Generate full code suggestions
        ai_full_code_suggestion = generate_full_code_suggestion(problem_description, code, is_correct_description=False)
        
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
                
                # Generate full code suggestion based on correct description
                correct_full_code_suggestion = generate_full_code_suggestion(correct_problem_description, code, is_correct_description=True)
                
                # Generate suggestion based on correct problem description
                correct_desc_entry = AIGuessEntry(
                    problem_analysis=correct_problem_description,
                    next_line_suggestion=correct_next_line,  # FIXED: Use correct next line
                    full_code_suggestion=correct_full_code_suggestion,
                    timestamp=datetime.now().isoformat(),
                    analysis_type="correct_description"
                )
                session_log.correct_description_history.append(correct_desc_entry)
            
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

class SubmitRequest(BaseModel):
    code: str
    user_id: str = None
    assignment_key: str = None
    session_id: str = None
    timestamp: str = None
    suggestion_clicked: int = 0
    compiler_clicked: int = 0

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
                success = await save_session_to_firebase(session_log, suggestion_clicked, compiler_clicked)
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