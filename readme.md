# AI Code Assistant

A real-time AI-powered Python code editor with intelligent suggestions and code execution capabilities.

**Created by:** Rahad Arman Nabid  
*PhD Student, Temple University*

## Features

- **Real-time AI Analysis** - Identifies problems and suggests next lines as you type
- **Code Execution** - Run Python code directly in the browser terminal
- **Syntax Highlighting** - VS Code-like Python syntax highlighting
- **User Authentication** - Secure login with user ID and assignment keys
- **Code Submission** - Submit assignments with automatic logging

## Setup

1. **Install dependencies**
   ```bash
   pip install fastapi uvicorn openai python-dotenv jinja2
   ```

2. **Environment variables**
   Create a `.env` file:
   ```
   OPENAI_API_KEY=your_openai_api_key_here
   APP_TITLE=AI Code Assistant API
   API_HOST=0.0.0.0
   API_PORT=8000
   ```

3. **Run the application**
   ```bash
   python main.py
   ```

4. **Access the app**
   - Frontend: http://localhost:8000
   - API Docs: http://localhost:8000/docs

## Usage

1. Enter your User ID and Assignment Key
2. Write Python code in the editor
3. Get real-time AI suggestions for next lines
4. Run code using the terminal (Ctrl+Enter)
5. Submit your final solution

## File Structure

```
├── main.py              # FastAPI backend
├── templates/
│   └── index.html       # Frontend interface
├── submissions/         # Saved code submissions
└── .env                 # Environment variables
```

## Keyboard Shortcuts

- `Ctrl+Enter` or `F5` - Run code
- `Ctrl+Shift+C` - Clear terminal
- `Ctrl+Shift+S` - Submit code
- `Tab` - Indent code
- `Ctrl+/` - Toggle comment

## Requirements

- Python 3.7+
- OpenAI API key
- Modern web browser