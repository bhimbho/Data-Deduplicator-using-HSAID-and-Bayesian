# HSAIDS - Hierarchical Sketches Assisted Inline De-duplication Scheme

A high-performance duplicate file detection system that uses advanced probabilistic data structures and machine learning techniques to efficiently find duplicate files, even in large-scale file systems.

## Overview

HSAIDS is an intelligent duplicate file finder that combines multiple optimization techniques to achieve fast and memory-efficient duplicate detection. Unlike traditional approaches that compare every file to every other file, HSAIDS uses hierarchical memory layers, probabilistic data structures, and Bayesian learning to find duplicates quickly.

## Key Features

- **Hierarchical Memory System**: Two-layer architecture (hot in RAM, cold in SSD) for optimal performance
- **Frequency-Sensitive Bloom Filters**: Adapts filter size based on file frequency to reduce false positives
- **Bayesian Search Optimization**: Learns and adapts lookup strategies over time for better performance
- **Container Grouping**: Organizes files into groups for improved data locality
- **Garbage Collection**: Efficient cleanup and duplicate detection during memory reclamation
- **Recursive Directory Scanning**: Scans all subdirectories automatically
- **Comprehensive Reporting**: Generates detailed CSV reports with statistics

## Requirements

- Python 3.6+
- Required packages:
  - `pandas` (for CSV output)

Install dependencies:
```bash
pip install pandas
```

## Usage

### Command Line

```bash
# Scan current directory
python hard_disk_hsad.py

# Scan a specific directory
python hard_disk_hsad.py /path/to/directory

# Include hidden files and directories
python hard_disk_hsad.py --include-hidden

# Disable garbage collection
python hard_disk_hsad.py --no-gc

# Show verbose progress
python hard_disk_hsad.py --verbose

# Don't save results to CSV
python hard_disk_hsad.py --no-save
```

### Python Script

```python
from hard_disk_hsad import find_duplicate_files

# Find duplicates in a directory
duplicate_files, unique_files, duplicate_groups, stats = find_duplicate_files(
    scan_path="/path/to/directory",
    include_hidden=False,
    enable_gc=True,
    verbose=True
)
```

### Jupyter Notebook

Use `hsaids.ipynb` for interactive exploration and visualization of the duplicate detection process.

## Output Files

The script generates several CSV files with detailed results:

- **`duplicate_files.csv`**: List of all duplicate files with metadata
- **`unique_files.csv`**: List of unique files (no duplicates found)
- **`duplicate_groups.csv`**: Files grouped by identical content
- **`hsaids_statistics.csv`**: Performance metrics and system statistics

## How It Works

HSAIDS uses a multi-phase approach:

1. **File Hashing**: Each file is hashed using MD5 to create a unique fingerprint
2. **Hierarchical Lookup**: Checks hot layer (RAM) first, then cold layer (SSD) if needed
3. **Probabilistic Filtering**: Uses Bloom filters to quickly determine if a file might be a duplicate
4. **Frequency Tracking**: Count-Min Sketch tracks how often each file appears
5. **Bayesian Learning**: Adapts lookup strategy based on success patterns
6. **Container Grouping**: Organizes similar files together for efficient searching
7. **Garbage Collection**: Periodically cleans up and finds additional duplicates

For a detailed explanation of the algorithms and concepts, see [HSAIDS_EXPLANATION.md](HSAIDS_EXPLANATION.md).

## Performance Characteristics

- **Time Complexity**: O(n) - linear with number of files
- **Memory Efficient**: Uses probabilistic data structures instead of storing all file data
- **Self-Optimizing**: Bayesian optimizer improves performance over time
- **Scalable**: Handles millions of files efficiently

## Example Output

```
🔍 Scanning directory (recursive): /path/to/directory
🚀 Initializing HSAIDS...
📁 Found 10,000 files to process
🔄 Processing files with HSAIDS...
  Processed 100/10000 files... (Confidence: 0.85, Groups: 15)
  ...
✅ Processing complete!
   Unique files: 8,500
   Duplicate files: 1,500
   Duplicate file groups: 300

📈 HSAIDS Statistics:
   Bayesian confidence: 0.961
   Container groups: 42
   GC objects reclaimed: 1,234
```

## Files in This Repository

- **`hsaids.py`**: Core HSAIDS implementation with all algorithms
- **`hard_disk_hsad.py`**: Main script for finding duplicate files
- **`hsaids.ipynb`**: Jupyter notebook for interactive use
- **`HSAIDS_EXPLANATION.md`**: Detailed explanation of how HSAIDS works

## License

This project is provided as-is for educational and research purposes.

## Contributing

Contributions, issues, and feature requests are welcome!

