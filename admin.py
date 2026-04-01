from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends, Header
from fastapi.responses import JSONResponse
from typing import Dict
from pathlib import Path
import json
import os
import shutil
from datetime import datetime
import re

router = APIRouter(prefix="/admin", tags=["admin"])

# Admin authentication
def verify_admin_token(authorization: str = Header(None)):
    """Verify admin access token"""
    if not authorization:
        raise HTTPException(status_code=401, detail="No authorization header")
    
    # Extract token from "Bearer TOKEN" format
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization format")
    
    token = authorization.replace("Bearer ", "")
    teacher_code = os.getenv("TEACHER_ACCESS_CODE", "")
    
    if not teacher_code or token != teacher_code:
        raise HTTPException(status_code=403, detail="Invalid admin credentials")
    
    return True

# ===== FILE UPLOAD ENDPOINTS =====

@router.post("/upload-assignment")
async def upload_assignment(
    assignment_key: str = Form(...),
    pdf_file: UploadFile = File(...),
    txt_file: UploadFile = File(...),
    survey_file: UploadFile = File(...),
    _: bool = Depends(verify_admin_token)
):
    """
    Upload assignment files (PDF, TXT, and Survey JSON)
    
    - assignment_key: Unique identifier for the assignment (e.g., "LARP1", "zenith")
    - pdf_file: Assignment PDF file
    - txt_file: Assignment description in markdown format (.txt)
    - survey_file: Survey configuration in JSON format
    """
    try:
        # Validate assignment key
        if not assignment_key or len(assignment_key) < 3:
            raise HTTPException(status_code=400, detail="Assignment key must be at least 3 characters")
        
        # Clean assignment key (remove special characters except underscore and hyphen)
        clean_key = re.sub(r'[^a-zA-Z0-9_-]', '', assignment_key)
        if clean_key != assignment_key:
            raise HTTPException(status_code=400, detail="Assignment key contains invalid characters")
        
        # Validate file types
        if not (pdf_file.filename or "").endswith('.pdf'):
            raise HTTPException(status_code=400, detail="PDF file must have .pdf extension")
        
        if not (txt_file.filename or "").endswith('.txt'):
            raise HTTPException(status_code=400, detail="TXT file must have .txt extension")
        
        if not (survey_file.filename or "").endswith('.json'):
            raise HTTPException(status_code=400, detail="Survey file must have .json extension")
        
        # Create directories if they don't exist
        assignment_dir = Path("assignment")
        surveys_dir = Path("surveys")
        assignment_dir.mkdir(exist_ok=True)
        surveys_dir.mkdir(exist_ok=True)
        
        # Validate and parse survey JSON
        survey_content = await survey_file.read()
        try:
            survey_data = json.loads(survey_content)
            
            # Validate survey structure
            if not isinstance(survey_data, dict):
                raise ValueError("Survey must be a JSON object")
            
            # Check for required survey sections
            if 'pre_survey' not in survey_data and 'post_survey' not in survey_data and 'periodic_survey' not in survey_data:
                raise ValueError("Survey must contain at least one of: pre_survey, post_survey, or periodic_survey")
            
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON in survey file: {str(e)}")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        
        # Read and validate markdown content
        txt_content = await txt_file.read()
        try:
            markdown_text = txt_content.decode('utf-8')
            if len(markdown_text.strip()) < 10:
                raise HTTPException(status_code=400, detail="Markdown content is too short (minimum 10 characters)")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="TXT file must be valid UTF-8 encoded text")
        
        # Save PDF file
        pdf_path = assignment_dir / f"{clean_key}.pdf"
        pdf_content = await pdf_file.read()
        with open(pdf_path, "wb") as f:
            f.write(pdf_content)
        
        # Save TXT file
        txt_path = assignment_dir / f"{clean_key}.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)
        
        # Save Survey JSON
        survey_path = surveys_dir / f"{clean_key}.json"
        with open(survey_path, "w", encoding="utf-8") as f:
            json.dump(survey_data, f, indent=2)
        
        return JSONResponse(content={
            "status": "success",
            "message": "Assignment files uploaded successfully",
            "assignment_key": clean_key,
            "files": {
                "pdf": str(pdf_path),
                "txt": str(txt_path),
                "survey": str(survey_path)
            },
            "details": {
                "pdf_size": len(pdf_content),
                "txt_lines": len(markdown_text.split('\n')),
                "survey_sections": list(survey_data.keys())
            },
            "timestamp": datetime.now().isoformat()
        })
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")


@router.get("/assignments")
async def list_assignments(_: bool = Depends(verify_admin_token)):
    """List all uploaded assignments"""
    try:
        assignment_dir = Path("assignment")
        surveys_dir = Path("surveys")
        
        if not assignment_dir.exists():
            return {"assignments": []}
        
        assignments = {}
        
        # Find all PDF files
        for pdf_file in assignment_dir.glob("*.pdf"):
            key = pdf_file.stem
            txt_file = assignment_dir / f"{key}.txt"
            survey_file = surveys_dir / f"{key}.json"
            
            assignments[key] = {
                "assignment_key": key,
                "files": {
                    "pdf": pdf_file.exists(),
                    "txt": txt_file.exists(),
                    "survey": survey_file.exists()
                },
                "complete": all([pdf_file.exists(), txt_file.exists(), survey_file.exists()])
            }
        
        return {
            "total": len(assignments),
            "assignments": list(assignments.values())
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list assignments: {str(e)}")


@router.delete("/assignment/{assignment_key}")
async def delete_assignment(
    assignment_key: str,
    _: bool = Depends(verify_admin_token)
):
    """Delete an assignment and all its files"""
    try:
        assignment_dir = Path("assignment")
        surveys_dir = Path("surveys")
        
        deleted_files = []
        
        # Delete PDF
        pdf_path = assignment_dir / f"{assignment_key}.pdf"
        if pdf_path.exists():
            pdf_path.unlink()
            deleted_files.append(str(pdf_path))
        
        # Delete TXT
        txt_path = assignment_dir / f"{assignment_key}.txt"
        if txt_path.exists():
            txt_path.unlink()
            deleted_files.append(str(txt_path))
        
        # Delete Survey
        survey_path = surveys_dir / f"{assignment_key}.json"
        if survey_path.exists():
            survey_path.unlink()
            deleted_files.append(str(survey_path))
        
        if not deleted_files:
            raise HTTPException(status_code=404, detail="Assignment not found")
        
        return {
            "status": "success",
            "message": f"Assignment '{assignment_key}' deleted",
            "deleted_files": deleted_files
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")


# ===== ENV FILE MANAGEMENT ENDPOINTS =====

@router.get("/env")
async def get_env_variables(_: bool = Depends(verify_admin_token)):
    """Get all environment variables (sensitive values masked)"""
    try:
        env_file = Path(".env")
        if not env_file.exists():
            return {"variables": {}, "message": ".env file not found"}
        
        variables = {}
        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        
                        # Mask sensitive values
                        sensitive_keys = ["API_KEY", "PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY"]
                        is_sensitive = any(s in key.upper() for s in sensitive_keys)
                        
                        if is_sensitive and value:
                            # Show first and last 4 characters
                            if len(value) > 8:
                                variables[key] = f"{value[:4]}...{value[-4:]}"
                            else:
                                variables[key] = "****"
                        else:
                            variables[key] = value
        
        return {
            "total": len(variables),
            "variables": variables
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read .env: {str(e)}")


@router.get("/env/{key}")
async def get_env_variable(
    key: str,
    _: bool = Depends(verify_admin_token)
):
    """Get a specific environment variable"""
    try:
        value = os.getenv(key)
        
        if value is None:
            raise HTTPException(status_code=404, detail=f"Variable '{key}' not found")
        
        # Mask sensitive values
        sensitive_keys = ["API_KEY", "PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY"]
        is_sensitive = any(s in key.upper() for s in sensitive_keys)
        
        if is_sensitive:
            masked_value = f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "****"
            return {
                "key": key,
                "value": masked_value,
                "masked": True,
                "message": "Use full=true parameter to see full value"
            }
        
        return {
            "key": key,
            "value": value,
            "masked": False
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get variable: {str(e)}")


@router.post("/env")
async def add_or_update_env_variable(
    key: str = Form(...),
    value: str = Form(...),
    _: bool = Depends(verify_admin_token)
):
    """Add or update an environment variable"""
    try:
        # Validate key format
        if not re.match(r'^[A-Z_][A-Z0-9_]*$', key):
            raise HTTPException(
                status_code=400, 
                detail="Key must start with a letter or underscore and contain only uppercase letters, numbers, and underscores"
            )
        
        env_file = Path(".env")
        backup_file = Path(f".env.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        
        # Backup current .env
        if env_file.exists():
            shutil.copy(env_file, backup_file)
        
        # Read existing variables
        lines = []
        key_found = False
        
        if env_file.exists():
            with open(env_file, "r") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        existing_key = stripped.split("=", 1)[0].strip()
                        if existing_key == key:
                            # Update existing key
                            lines.append(f"{key}={value}\n")
                            key_found = True
                        else:
                            lines.append(line)
                    else:
                        lines.append(line)
        
        # Add new key if not found
        if not key_found:
            lines.append(f"\n# Added via admin panel\n{key}={value}\n")
        
        # Write back to .env
        with open(env_file, "w") as f:
            f.writelines(lines)
        
        # Update environment variable in current process
        os.environ[key] = value
        
        return {
            "status": "success",
            "message": f"Variable '{key}' {'updated' if key_found else 'added'}",
            "key": key,
            "action": "updated" if key_found else "added",
            "backup": str(backup_file),
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update variable: {str(e)}")


@router.delete("/env/{key}")
async def delete_env_variable(
    key: str,
    _: bool = Depends(verify_admin_token)
):
    """Delete an environment variable"""
    try:
        env_file = Path(".env")
        if not env_file.exists():
            raise HTTPException(status_code=404, detail=".env file not found")
        
        # Backup current .env
        backup_file = Path(f".env.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy(env_file, backup_file)
        
        # Read and filter lines
        lines = []
        key_found = False
        
        with open(env_file, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    existing_key = stripped.split("=", 1)[0].strip()
                    if existing_key == key:
                        key_found = True
                        continue  # Skip this line
                lines.append(line)
        
        if not key_found:
            raise HTTPException(status_code=404, detail=f"Variable '{key}' not found")
        
        # Write back to .env
        with open(env_file, "w") as f:
            f.writelines(lines)
        
        # Remove from current process environment
        os.environ.pop(key, None)
        
        return {
            "status": "success",
            "message": f"Variable '{key}' deleted",
            "key": key,
            "backup": str(backup_file),
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete variable: {str(e)}")


@router.post("/env/bulk")
async def bulk_update_env(
    variables: Dict[str, str],
    _: bool = Depends(verify_admin_token)
):
    """Bulk update multiple environment variables"""
    try:
        if not variables:
            raise HTTPException(status_code=400, detail="No variables provided")
        
        # Validate all keys first
        for key in variables.keys():
            if not re.match(r'^[A-Z_][A-Z0-9_]*$', key):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid key format: '{key}'. Must be uppercase with underscores only."
                )
        
        env_file = Path(".env")
        backup_file = Path(f".env.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        
        # Backup current .env
        if env_file.exists():
            shutil.copy(env_file, backup_file)
        
        # Read existing content
        existing_vars = {}
        other_lines = []
        
        if env_file.exists():
            with open(env_file, "r") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        k, v = stripped.split("=", 1)
                        existing_vars[k.strip()] = v.strip()
                    else:
                        other_lines.append(line)
        
        # Update with new values
        for key, value in variables.items():
            existing_vars[key] = value
            os.environ[key] = value
        
        # Write back
        with open(env_file, "w") as f:
            # Write comments and empty lines first
            for line in other_lines:
                f.write(line)
            
            # Write all variables
            f.write("\n# Variables\n")
            for key, value in sorted(existing_vars.items()):
                f.write(f"{key}={value}\n")
        
        return {
            "status": "success",
            "message": f"Updated {len(variables)} variables",
            "updated_keys": list(variables.keys()),
            "backup": str(backup_file),
            "timestamp": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Bulk update failed: {str(e)}")


@router.get("/env/backups")
async def list_env_backups(_: bool = Depends(verify_admin_token)):
    """List all .env backup files"""
    try:
        backups = []
        for backup_file in Path(".").glob(".env.backup.*"):
            backups.append({
                "filename": backup_file.name,
                "created": datetime.fromtimestamp(backup_file.stat().st_mtime).isoformat(),
                "size": backup_file.stat().st_size
            })
        
        backups.sort(key=lambda x: x['created'], reverse=True)
        
        return {
            "total": len(backups),
            "backups": backups
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list backups: {str(e)}")