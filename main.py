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

# Load environment variables
load_dotenv()

# Add logging and debugging capabilities
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title=os.getenv("APP_TITLE", "AI Code Assistant API"))

# Enable CORS for frontend - MUST be before other middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),  # In production, specify exact origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add request tracking for continuous analysis
analysis_history = []

def add_to_history(code, analysis, next_line):
    """Track analysis history for better continuous suggestions"""
    global analysis_history
    analysis_history.append({
        "timestamp": __import__("datetime").datetime.now(),
        "code_length": len(code),
        "code_hash": hash(code),
        "analysis_preview": analysis[:100] + "..." if len(analysis) > 100 else analysis,
        "next_line_preview": next_line[:50] + "..." if len(next_line) > 50 else next_line
    })
    
    # Keep only last 10 analyses
    if len(analysis_history) > 10:
        analysis_history.pop(0)

# Set up templates directory
templates = Jinja2Templates(directory="templates")

# OpenAI Configuration
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    raise ValueError("OPENAI_API_KEY not found in environment variables")

client = OpenAI(api_key=API_KEY)

def get_response(prompt):
    """Get response from OpenAI GPT model"""
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "system", "content": prompt}],
            model="gpt-4o-mini",
        )
        return chat_completion.choices[0].message.content
    except Exception as e:
        return f"Error getting AI response: {str(e)}"

def analyze_problem_statement(existing_code):
    """Analyze what problem the programmer is trying to solve"""
    what_problem_is_this = f"""
    You are an expert code analyst. Analyze the following code and provide a brief problem description:

    ```python
    {existing_code}
    ```

    Provide ONLY a brief description of what problem this code is trying to solve. Keep it concise (1-3 sentences max).
    
    Examples of good responses:
    - "This code implements a factorial function using recursion"
    - "Building a sorting algorithm to arrange numbers in ascending order"
    - "Creating a class to represent a data structure with methods"
    - "Implementing a search function to find elements in a list"
    
    Do NOT include sections, bullet points, or markdown formatting. Just a simple, clear description.
    """
    
    problem_description = get_response(what_problem_is_this)
    return problem_description

def predict_next_line(problem_description, existing_code):
    """Predict the next line of code based on problem and existing code"""
    
    # Analyze the code structure to understand context
    lines = existing_code.strip().split('\n')
    last_line = lines[-1] if lines else ""
    
    # Check current indentation level
    current_indent = len(last_line) - len(last_line.lstrip())
    
    what_is_the_next_line = f"""
    You are an expert Python programmer. Based on this problem: {problem_description}
    
    Current code:
    ```python
    {existing_code}
    ```
    
    Generate ONLY the next single line of code that should logically follow. 
    
    Rules:
    - Provide EXACTLY ONE line of code (maximum 2 lines if absolutely necessary)
    - Maintain proper Python indentation using spaces
    - No explanations, comments, or multiple code blocks
    - Focus on the immediate next logical step
    - If the code structure needs completion (like an else clause), provide just that
    
    Examples of good responses:
    - "    else:"
    - "        return n * factorial(n - 1)"
    - "    pass"
    - "def another_function():"
    
    Next line:
    """
    
    next_line = get_response(what_is_the_next_line)
    return next_line

def execute_python_code(code, timeout=None):
    """
    Execute Python code safely in a subprocess with timeout
    Returns: (output, error, execution_time, return_value)
    """
    if timeout is None:
        timeout = int(os.getenv("EXECUTION_TIMEOUT", "10"))
    
    start_time = time.time()
    
    try:
        # Create a temporary file for the code
        temp_dir = tempfile.mkdtemp()
        temp_file = Path(temp_dir) / f"user_code_{uuid.uuid4().hex[:8]}.py"
        
        # Write the code to the temporary file
        with open(temp_file, 'w', encoding='utf-8') as f:
            f.write(code)
        
        # Execute the code using subprocess
        result = subprocess.run(
            [sys.executable, str(temp_file)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=temp_dir
        )
        
        execution_time = time.time() - start_time
        
        # Clean up
        temp_file.unlink()
        temp_dir = Path(temp_dir)
        if temp_dir.exists():
            import shutil
            shutil.rmtree(temp_dir)
        
        return {
            "output": result.stdout,
            "error": result.stderr,
            "execution_time": execution_time,
            "return_code": result.returncode
        }
        
    except subprocess.TimeoutExpired:
        execution_time = time.time() - start_time
        return {
            "output": "",
            "error": f"Code execution timed out after {timeout} seconds",
            "execution_time": execution_time,
            "return_code": -1
        }
    except Exception as e:
        execution_time = time.time() - start_time
        return {
            "output": "",
            "error": f"Execution error: {str(e)}",
            "execution_time": execution_time,
            "return_code": -1
        }

class CodeRequest(BaseModel):
    code: str
    user_id: str = None
    assignment_key: str = None

class CodeResponse(BaseModel):
    analysis: str
    next_line: str

class ExecuteRequest(BaseModel):
    code: str
    user_id: str = None
    assignment_key: str = None

class ExecuteResponse(BaseModel):
    output: str
    error: str
    execution_time: float
    result: str = ""  # Default to empty string instead of None

class SubmitRequest(BaseModel):
    code: str
    user_id: str = None
    assignment_key: str = None
    timestamp: str = None

class SubmitResponse(BaseModel):
    status: str
    message: str
    submission_id: str
    timestamp: str

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main HTML page"""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api")
async def api_root():
    return {"message": "AI Code Assistant API is running"}

@app.post("/analyze", response_model=CodeResponse)
async def analyze_code(request: CodeRequest):
    """
    Continuously analyze the provided code using OpenAI and return analysis + next line suggestion
    """
    try:
        code = request.code.strip()
        
        if len(code) > int(os.getenv("MAX_CODE_LENGTH", "5000")):  # Limit code length for API efficiency
            raise HTTPException(status_code=400, detail=f"Code too long - please keep under {os.getenv('MAX_CODE_LENGTH', '5000')} characters")
        
        if not code:
            return CodeResponse(
                analysis="Start typing code to get AI-powered problem analysis...",
                next_line="def example():\n    pass"
            )
        
        # Enhanced analysis for very short code snippets
        if len(code.strip()) < 10:
            return CodeResponse(
                analysis=f"Code snippet in progress: {code.strip()}",
                next_line="# Continue your statement"
            )
        
        # Step 1: Analyze what problem the code is trying to solve
        print(f"🔍 Analyzing code: {code[:50]}..." if len(code) > 50 else f"🔍 Analyzing code: {code}")
        problem_description = analyze_problem_statement(code)
        
        # Clean problem description (remove any unwanted formatting)
        problem_description = re.sub(r'^\*\*.*?\*\*:\s*', '', problem_description)  # Remove markdown headers
        problem_description = re.sub(r'^Problem.*?:\s*', '', problem_description, flags=re.IGNORECASE)
        problem_description = problem_description.strip()
        
        # Step 2: Predict the next line based on problem and existing code
        print("🤖 Generating next line suggestion...")
        next_line = predict_next_line(problem_description, code)
        
        # Enhanced cleaning for single line output
        next_line = re.sub(r'```python\n?', '', next_line)
        next_line = re.sub(r'```\w*\n?', '', next_line)
        next_line = re.sub(r'```', '', next_line)
        next_line = re.sub(r'^Next line[s]?:\s*', '', next_line, flags=re.IGNORECASE)
        next_line = re.sub(r'^Here[\'s\s]+.*?:\s*', '', next_line, flags=re.IGNORECASE)
        
        # Ensure only 1-2 lines maximum
        lines = next_line.strip().split('\n')
        if len(lines) > 2:
            next_line = '\n'.join(lines[:2])
        else:
            next_line = next_line.strip()
        
        # Remove any explanatory text that might remain
        if any(word in next_line.lower() for word in ['this will', 'this line', 'explanation', 'note:', 'comment:']):
            # If explanatory text found, try to extract just the code
            code_lines = []
            for line in lines:
                stripped = line.strip()
                if (stripped and 
                    not any(word in stripped.lower() for word in ['this will', 'this line', 'explanation', 'note:', 'comment:']) and
                    not stripped.lower().startswith(('the ', 'here', 'this '))):
                    code_lines.append(line)
                    if len(code_lines) >= 2:
                        break
            next_line = '\n'.join(code_lines) if code_lines else "# Continue here"
        
        # Ensure we have something useful
        if not next_line or len(next_line.strip()) < 2:
            next_line = "# Continue your implementation here"
        
        print(f"✅ Analysis complete. Next line: {next_line[:30]}...")
        
        # Add to analysis history
        add_to_history(code, problem_description, next_line)
        
        return CodeResponse(
            analysis=problem_description,
            next_line=next_line
        )
        
    except Exception as e:
        print(f"❌ Error in analysis: {str(e)}")
        # Enhanced fallback responses based on error type
        error_msg = str(e).lower()
        
        if "rate limit" in error_msg or "quota" in error_msg:
            fallback_analysis = """**⚠️ Rate Limit**: API temporarily unavailable

**📊 Status**: Too many requests - please wait a moment

**🔄 Solution**: Try again in a few seconds"""
        
        elif "network" in error_msg or "connection" in error_msg:
            fallback_analysis = """**⚠️ Connection Issue**: Network error

**📊 Status**: Unable to reach AI service

**🔄 Solution**: Check your internet connection"""
        
        else:
            fallback_analysis = f"""**⚠️ Analysis Error**: {str(e)[:100]}

**📊 Status**: Temporary AI service interruption

**🔄 Solution**: Continue coding - analysis will resume automatically"""
        
        return CodeResponse(
            analysis=fallback_analysis,
            next_line="# Analysis temporarily unavailable - continue coding"
        )

@app.post("/execute", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest):
    """
    Execute Python code safely and return output, errors, and execution time
    """
    try:
        code = request.code.strip()
        
        if not code:
            raise HTTPException(status_code=400, detail="No code provided for execution")
        
        if len(code) > int(os.getenv("MAX_CODE_LENGTH", "10000")):  # Limit code length for security
            raise HTTPException(status_code=400, detail=f"Code too long - please keep under {os.getenv('MAX_CODE_LENGTH', '10000')} characters")
        
        # Log execution attempt
        user_id = getattr(request, 'user_id', 'anonymous')
        print(f"🚀 Executing code for user: {user_id}")
        print(f"📝 Code preview: {code[:100]}..." if len(code) > 100 else f"📝 Code: {code}")
        
        # Execute the code safely
        execution_result = execute_python_code(code, timeout=int(os.getenv("EXECUTION_TIMEOUT", "10")))
        
        # Prepare response
        output = execution_result.get("output", "")
        error = execution_result.get("error", "")
        execution_time = execution_result.get("execution_time", 0)
        return_code = execution_result.get("return_code", 0)
        
        # Clean up output and error messages
        output = output.strip() if output else ""
        error = error.strip() if error else ""
        
        # Debug logging
        print(f"📤 Raw output: '{output}'")
        print(f"📤 Raw error: '{error}'")
        print(f"📤 Return code: {return_code}")
        
        # Determine if execution was successful
        success = return_code == 0 and not error
        
        print(f"✅ Execution completed in {execution_time:.3f}s - Success: {success}")
        
        # For successful executions with no output, indicate completion
        result_message = ""
        if success and not output:
            result_message = "Code executed successfully (no output)"
        
        response_data = ExecuteResponse(
            output=output,
            error=error,
            execution_time=round(execution_time, 3),
            result=result_message
        )
        
        # Debug the final response
        print(f"📤 Final response: output='{response_data.output}', error='{response_data.error}', time={response_data.execution_time}")
        
        return response_data
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in code execution: {str(e)}")
        return ExecuteResponse(
            output="",
            error=f"Server error during code execution: {str(e)}",
            execution_time=0.0,
            result=""
        )

@app.post("/submit", response_model=SubmitResponse)
async def submit_code(request: SubmitRequest):
    """
    Submit final code for assignment/quiz and log the submission
    """
    try:
        code = request.code.strip()
        user_id = request.user_id or "anonymous"
        assignment_key = request.assignment_key or "unknown"
        timestamp = request.timestamp or __import__("datetime").datetime.now().isoformat()
        
        if not code:
            raise HTTPException(status_code=400, detail="No code provided for submission")
        
        if len(code) > int(os.getenv("MAX_SUBMISSION_LENGTH", "15000")):  # Higher limit for final submissions
            raise HTTPException(status_code=400, detail=f"Code too long - please keep under {os.getenv('MAX_SUBMISSION_LENGTH', '15000')} characters")
        
        # Generate submission ID
        submission_id = f"{user_id}_{assignment_key}_{uuid.uuid4().hex[:8]}"
        
        # Log submission
        print(f"📤 CODE SUBMISSION:")
        print(f"   User ID: {user_id}")
        print(f"   Assignment Key: {assignment_key}")
        print(f"   Submission ID: {submission_id}")
        print(f"   Timestamp: {timestamp}")
        print(f"   Code Length: {len(code)} characters")
        print(f"   Code Preview: {code[:200]}..." if len(code) > 200 else f"   Code: {code}")
        print(f"   {'='*50}")
        
        # Save submission to file (in production, you'd save to database)
        try:
            submissions_dir = Path(os.getenv("SUBMISSIONS_DIR", "submissions"))
            submissions_dir.mkdir(exist_ok=True)
            
            submission_file = submissions_dir / f"{submission_id}.py"
            with open(submission_file, 'w', encoding='utf-8') as f:
                f.write(f"# Submission Details\n")
                f.write(f"# User ID: {user_id}\n")
                f.write(f"# Assignment Key: {assignment_key}\n")
                f.write(f"# Submission ID: {submission_id}\n")
                f.write(f"# Timestamp: {timestamp}\n")
                f.write(f"# Code Length: {len(code)} characters\n")
                f.write(f"#{'='*50}\n\n")
                f.write(code)
            
            print(f"✅ Submission saved to: {submission_file}")
            
        except Exception as save_error:
            print(f"⚠️ Failed to save submission file: {save_error}")
            # Continue even if file saving fails
        
        # Also test execution of submitted code
        execution_result = execute_python_code(code, timeout=15)
        execution_success = execution_result.get("return_code", -1) == 0 and not execution_result.get("error", "").strip()
        
        print(f"🔍 Execution Test: {'✅ Success' if execution_success else '❌ Failed'}")
        if execution_result.get("error"):
            print(f"   Error: {execution_result.get('error')[:200]}...")
        
        return SubmitResponse(
            status="success",
            message=f"Code submitted successfully. Submission ID: {submission_id}",
            submission_id=submission_id,
            timestamp=timestamp
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in code submission: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Submission failed: {str(e)}")

@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "AI Code Assistant"}

@app.get("/analysis-history")
async def get_analysis_history():
    """Get recent analysis history for debugging"""
    return {
        "total_analyses": len(analysis_history),
        "recent_analyses": analysis_history[-5:] if analysis_history else [],
        "status": "tracking_enabled"
    }

@app.get("/stats")
async def get_stats():
    """Get API statistics"""
    return {
        "total_analyses_performed": len(analysis_history),
        "api_status": "operational",
        "openai_model": "gpt-4o-mini",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "features": [
            "continuous_analysis",
            "markdown_formatting", 
            "syntax_aware_suggestions",
            "context_aware_next_line",
            "error_handling",
            "code_execution",
            "user_authentication"
        ]
    }

@app.get("/test-execution")
async def test_execution():
    """Test endpoint to verify code execution works"""
    test_code = """
print("Hello from AI Code Assistant!")
print("Python is working correctly!")

# Test basic operations
x = 5
y = 3
print(f"5 + 3 = {x + y}")

# Test function
def greet(name):
    return f"Hello, {name}!"

print(greet("World"))
"""
    
    result = execute_python_code(test_code)
    return {
        "test_status": "completed",
        "execution_result": result,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    }

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting AI Code Assistant API with Code Execution...")
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    print(f"📝 Frontend: http://localhost:{port}")
    print(f"🔗 API Docs: http://localhost:{port}/docs")
    print(f"❤️ Health Check: http://localhost:{port}/health")
    print(f"🧪 Test Execution: http://localhost:{port}/test-execution")
    print("🐍 Python Version:", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    
    uvicorn.run(
        app, 
        host=host, 
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info"),
        reload=os.getenv("API_RELOAD", "True").lower() == "true"
    )