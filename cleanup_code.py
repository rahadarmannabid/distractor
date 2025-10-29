#!/usr/bin/env python3
"""
Code cleanup script to remove verbose logging, comments, and polish code for production.
This script will:
1. Remove excessive console.log statements (keep only error/critical logs)
2. Remove comment markers and section dividers
3. Remove TODO/FIXME comments
4. Remove redundant explanatory comments
5. Create backup files before modification
"""

import re
import os
from datetime import datetime

def create_backup(filepath):
    """Create a timestamped backup of the file"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{filepath}.backup_{timestamp}"
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✓ Backup created: {backup_path}")
    return backup_path

def cleanup_index_html(filepath):
    """Clean up index.html file"""
    print(f"\n{'='*60}")
    print(f"Cleaning: {filepath}")
    print(f"{'='*60}\n")
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_lines = content.count('\n')
    
    # Remove verbose console.log (keep only error, warn, and critical info)
    # This pattern finds standalone console.log lines but preserves error/warn
    patterns_to_remove = [
        # Console.log statements (but keep console.error, console.warn)
        (r'^\s*console\.log\([^)]*\);\s*\n', '', 'Verbose console.log statements'),
        
        # Section divider comments
        (r'^\s*//\s*[=\-_]{3,}.*\n', '', 'Section divider comments'),
        (r'^\s*/\*\s*[=\-_]{3,}.*\*/\s*\n', '', 'Block section dividers'),
        
        # TODO/FIXME/NOTE comments
        (r'^\s*//\s*(TODO|FIXME|NOTE|HACK|XXX):?.*\n', '', 'TODO/FIXME markers'),
        
        # Standalone comment lines that are purely explanatory
        (r'^\s*//\s*Step \d+:.*\n', '', 'Step comments'),
        (r'^\s*//\s*Debug:.*\n', '', 'Debug comments'),
        (r'^\s*//\s*Log .*\n', '', 'Log description comments'),
        (r'^\s*//\s*For debugging.*\n', '', 'Debugging comments'),
    ]
    
    removed_counts = {}
    
    for pattern, replacement, description in patterns_to_remove:
        matches = len(re.findall(pattern, content, re.MULTILINE))
        if matches > 0:
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            removed_counts[description] = matches
    
    # Remove duplicate empty lines (max 2 consecutive)
    content = re.sub(r'\n{4,}', '\n\n\n', content)
    
    final_lines = content.count('\n')
    
    # Display summary
    print("Cleanup Summary:")
    print("-" * 60)
    for desc, count in removed_counts.items():
        print(f"  {desc}: {count}")
    print(f"\nTotal lines: {original_lines} → {final_lines} ({original_lines - final_lines} lines removed)")
    
    return content, removed_counts

def cleanup_main_py(filepath):
    """Clean up main.py file"""
    print(f"\n{'='*60}")
    print(f"Cleaning: {filepath}")
    print(f"{'='*60}\n")
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_lines = content.count('\n')
    
    patterns_to_remove = [
        # Section divider comments
        (r'^\s*#\s*[=\-_]{3,}.*\n', '', 'Section dividers'),
        
        # TODO/FIXME markers
        (r'^\s*#\s*(TODO|FIXME|NOTE|HACK|XXX):?.*\n', '', 'TODO/FIXME markers'),
        
        # Explanatory comments that are redundant
        (r'^\s*#\s*Add this.*\n', '', 'Add this... comments'),
        (r'^\s*#\s*Use .*method.*\n', '', 'Method description comments'),
        (r'^\s*#\s*Configure .*\n', '', 'Configuration comments'),
        (r'^\s*#\s*Initialize .*\n', '', 'Initialization comments'),
    ]
    
    removed_counts = {}
    
    for pattern, replacement, description in patterns_to_remove:
        matches = len(re.findall(pattern, content, re.MULTILINE))
        if matches > 0:
            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            removed_counts[description] = matches
    
    # Remove duplicate empty lines (max 2 consecutive)
    content = re.sub(r'\n{4,}', '\n\n\n', content)
    
    final_lines = content.count('\n')
    
    print("Cleanup Summary:")
    print("-" * 60)
    for desc, count in removed_counts.items():
        print(f"  {desc}: {count}")
    print(f"\nTotal lines: {original_lines} → {final_lines} ({original_lines - final_lines} lines removed)")
    
    return content, removed_counts

def main():
    """Main cleanup function"""
    print("\n" + "="*60)
    print("CODE CLEANUP SCRIPT - Professional Code Polish")
    print("="*60)
    
    base_path = "/Users/rahadarmannabid/Documents/Autosugession Quiz/autosuggestion-quiz-v4"
    
    files_to_clean = [
        ("templates/index.html", cleanup_index_html),
        ("main.py", cleanup_main_py)
    ]
    
    total_removed = {}
    
    for filename, cleanup_func in files_to_clean:
        filepath = os.path.join(base_path, filename)
        
        if not os.path.exists(filepath):
            print(f"\n⚠ File not found: {filepath}")
            continue
        
        # Create backup
        backup = create_backup(filepath)
        
        # Clean up
        cleaned_content, counts = cleanup_func(filepath)
        
        # Merge counts
        for key, val in counts.items():
            total_removed[key] = total_removed.get(key, 0) + val
        
        # Prompt for confirmation
        print(f"\n{'='*60}")
        response = input(f"Apply changes to {filename}? (yes/no): ").strip().lower()
        
        if response in ['yes', 'y']:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(cleaned_content)
            print(f"✓ Changes applied to {filename}")
        else:
            print(f"✗ Changes discarded for {filename}")
            print(f"  Backup available at: {backup}")
    
    # Final summary
    print(f"\n{'='*60}")
    print("OVERALL CLEANUP SUMMARY")
    print(f"{'='*60}")
    for desc, count in sorted(total_removed.items()):
        print(f"  {desc}: {count}")
    print(f"\n✓ Cleanup complete!")
    print(f"✓ Backups created with .backup_TIMESTAMP extension")
    print(f"\nTo restore from backup: cp <backup_file> <original_file>")

if __name__ == "__main__":
    main()
