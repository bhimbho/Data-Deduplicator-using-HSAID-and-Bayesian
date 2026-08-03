#!/usr/bin/env python3
"""
HSAIDS-based Duplicate File Finder

An Enhanced Duplicate Detection and Efficient Storage Management System
using Hierarchical Sketch Assisted Inline De-duplication Scheme (HSAIDS).

Features:
- Hierarchical sketch layers (hot in RAM, cold in SSD)
- Frequency-sensitive Bloom filter allocation
- Bayesian search optimization
- Hierarchical clustering-based container groupings
- Sketch layer merging with adaptive frequency
- Efficient object ID to container group mapping
- Duplicate detection during Garbage Collection
"""

import os
import hashlib
from pathlib import Path
from hsaids.hsaids import HSAIDS
from collections import defaultdict
import pandas as pd


def hash_file(file_path):
    """
    Generate MD5 hash of file contents.
    Handles binary and text files safely.
    """
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            # Read file in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except (IOError, OSError, PermissionError) as e:
        print(f"⚠️  Error reading {file_path}: {e}")
        return None


def get_all_files(directory, include_hidden=False, verbose=False):
    """
    Recursively get all files in a directory and all subdirectories.
    Returns list of file paths.
    
    Args:
        directory: Path to directory to scan
        include_hidden: If True, includes hidden files/directories
        verbose: If True, prints progress information
    
    Returns:
        List of all file paths found recursively
    """
    files = []
    directories_scanned = 0
    
    # Ensure directory path is absolute and exists
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise ValueError(f"'{directory}' is not a valid directory")
    
    if verbose:
        print(f"📂 Starting recursive scan from: {directory}")
    
    # os.walk() recursively traverses all subdirectories
    for root, dirs, filenames in os.walk(directory):
        directories_scanned += 1
        
        if verbose and directories_scanned % 100 == 0:
            print(f"   Scanned {directories_scanned} directories, found {len(files)} files so far...")
        
        # Optionally skip hidden directories
        # Modifying dirs in-place prevents os.walk from descending into them
        if not include_hidden:
            dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        # Process all files in current directory
        for filename in filenames:
            # Optionally skip hidden files
            if include_hidden or not filename.startswith('.'):
                file_path = os.path.join(root, filename)
                # Only add if it's actually a file (not a directory or symlink to directory)
                if os.path.isfile(file_path):
                    files.append(file_path)
                elif verbose:
                    # Log if we encounter something that's not a regular file
                    if os.path.islink(file_path):
                        print(f"   ⚠️  Skipping symlink: {file_path}")
    
    if verbose:
        print(f"✅ Recursive scan complete: {directories_scanned} directories, {len(files)} files found")
    
    return files


def find_duplicate_files(scan_path, include_hidden=False, enable_gc=True, verbose=False):
    """
    Find duplicate files using HSAIDS (Hierarchical Sketch Assisted Inline De-duplication Scheme).
    Recursively scans all subdirectories.
    
    Args:
        scan_path: Directory path to scan (will recursively scan all subdirectories)
        include_hidden: Whether to include hidden files/directories
        enable_gc: Enable garbage collection during processing
        verbose: If True, shows detailed progress during directory scanning
    
    Returns:
        tuple: (duplicate_files, unique_files, duplicate_groups, hsaids_stats)
    """
    print(f"🔍 Scanning directory (recursive): {os.path.abspath(scan_path)}")
    print("🚀 Initializing HSAIDS (Hierarchical Sketch Assisted Inline De-duplication Scheme)...")
    
    # Get all files recursively from directory and all subdirectories
    all_files = get_all_files(scan_path, include_hidden, verbose=verbose)
    print(f"📁 Found {len(all_files)} files to process (from all subdirectories)")
    
    # Initialize HSAIDS with enhanced features
    hsaids = HSAIDS(
        hot_bloom_size=10000,
        hot_cms_width=5000,
        cold_bloom_size=100000,
        cold_cms_width=20000,
        threshold=2,
        promotion_threshold=5,
        merge_frequency=500  # Adaptive merging
    )
    
    # Dictionary to track files by their hash
    file_hash_map = defaultdict(list)
    file_to_object_id = {}  # Track file to object ID mapping
    object_id_to_file = {}  # Track object ID to file path mapping (reverse lookup)
    hash_to_files = defaultdict(list)  # Track hash to all files with that hash
    
    # Process each file
    duplicate_files = []  # List of (file_path, hash, object_id, hsaids_dups, same_hash_files, frequency, layer)
    unique_files = []      # List of (file_path, hash, object_id, group_id, frequency)
    
    print("🔄 Processing files with HSAIDS...")
    print("   Features: Frequency-sensitive BF, Bayesian optimization, Container grouping")
    
    for i, file_path in enumerate(all_files, 1):
        # Hash the file contents
        file_hash = hash_file(file_path)
        
        if file_hash is None:
            continue  # Skip files that couldn't be read
        
        # Track file by hash
        file_hash_map[file_hash].append(file_path)
        hash_to_files[file_hash].append(file_path)
        
        # Prepare metadata
        try:
            file_size = os.path.getsize(file_path)
            metadata = {
                'file_path': file_path,
                'file_size': file_size,
                'file_name': os.path.basename(file_path)
            }
        except OSError:
            metadata = {'file_path': file_path, 'file_name': os.path.basename(file_path)}
        
        # Check with HSAIDS (includes all optimizations)
        result = hsaids.insert(file_hash, metadata)
        
        # Track object ID mappings
        object_id = result.get("object_id")
        if object_id:
            file_to_object_id[file_path] = object_id
            object_id_to_file[object_id] = file_path
        
        # Determine if this is a duplicate based on:
        # 1. HSAIDS marked it as duplicate (frequency > threshold)
        # 2. OR there are other files with the same hash (actual duplicates)
        is_duplicate_by_hsaids = result["status"] == "duplicate"
        is_duplicate_by_hash = len(hash_to_files[file_hash]) > 1
        
        if is_duplicate_by_hsaids or is_duplicate_by_hash:
            # Map duplicate object IDs to file paths
            duplicate_object_ids = result.get("duplicate_objects", [])
            duplicate_file_paths = [
                object_id_to_file.get(obj_id, f"object_{obj_id}") 
                for obj_id in duplicate_object_ids
                if obj_id in object_id_to_file
            ]
            
            # Also include files with same hash from our tracking
            same_hash_files = [f for f in hash_to_files[file_hash] if f != file_path]
            
            duplicate_files.append((
                file_path, 
                file_hash, 
                object_id,
                duplicate_file_paths,  # Files found by HSAIDS
                same_hash_files,  # All files with same hash
                result.get("frequency", 0),
                result.get("layer", "unknown") if is_duplicate_by_hsaids else "hash_match"
            ))
        else:
            unique_files.append((
                file_path,
                file_hash,
                object_id,
                result.get("group_id"),
                result.get("frequency", 0)
            ))
        
        # Periodic garbage collection
        if enable_gc and i % 1000 == 0:
            gc_result = hsaids.garbage_collect(min_frequency=1)
            if gc_result['reclaimed'] > 0:
                print(f"  🗑️  GC: Reclaimed {gc_result['reclaimed']} objects, "
                      f"found {gc_result['duplicates_found']} duplicates")
        
        # Progress indicator
        if i % 100 == 0:
            stats = hsaids.get_statistics()
            print(f"  Processed {i}/{len(all_files)} files... "
                  f"(Confidence: {stats['bayesian_confidence']:.2f}, "
                  f"Groups: {stats['container_groups']})")
    
    # Final garbage collection
    if enable_gc:
        print("\n🧹 Running final garbage collection...")
        gc_result = hsaids.garbage_collect(min_frequency=1)
        print(f"   Reclaimed: {gc_result['reclaimed']} objects")
        print(f"   Duplicates found: {gc_result['duplicates_found']}")
    
    # Group duplicate files by hash
    duplicate_groups = {hash_val: paths for hash_val, paths in file_hash_map.items() 
                        if len(paths) > 1}
    
    # Get HSAIDS statistics
    stats = hsaids.get_statistics()
    
    print(f"\n✅ Processing complete!")
    print(f"   Unique files: {len(unique_files)}")
    print(f"   Duplicate files: {len(duplicate_files)}")
    print(f"   Duplicate file groups: {len(duplicate_groups)}")
    print(f"\n📈 HSAIDS Statistics:")
    print(f"   Total objects: {stats['total_objects']}")
    print(f"   Container groups: {stats['container_groups']}")
    print(f"   Bayesian confidence: {stats['bayesian_confidence']:.3f}")
    print(f"   Hot CMS updates: {stats['hot_cms_updates']}")
    print(f"   Cold CMS updates: {stats['cold_cms_updates']}")
    print(f"   Merge frequency: {stats['merge_frequency']}")
    print(f"   GC runs: {stats['gc_stats']['runs']}")
    print(f"   GC objects reclaimed: {stats['gc_stats']['objects_reclaimed']}")
    
    return duplicate_files, unique_files, duplicate_groups, stats


def save_results(duplicate_files, unique_files, duplicate_groups, hsaids_stats=None):
    """Save results to CSV files with HSAIDS metadata."""
    # Create DataFrames for duplicate and unique files
    duplicates_data = []
    for item in duplicate_files:
        if len(item) >= 6:
            file_path, file_hash, object_id, hsaids_duplicates, same_hash_files, frequency, layer = item[:7]
        elif len(item) >= 5:
            file_path, file_hash, object_id, duplicate_objects, frequency, layer = item[:6]
            hsaids_duplicates = duplicate_objects if isinstance(duplicate_objects, list) else []
            same_hash_files = []
        else:
            file_path, file_hash = item[:2]
            object_id, hsaids_duplicates, same_hash_files, frequency, layer = None, [], [], 0, "unknown"
        
        # Combine all duplicate file paths
        all_duplicate_files = list(set(hsaids_duplicates + same_hash_files))
        
        try:
            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        except OSError:
            file_size = 0
        
        duplicates_data.append({
            'file_path': file_path,
            'file_hash': file_hash,
            'object_id': object_id,
            'duplicate_count': len(all_duplicate_files),
            'hsaids_detected_count': len(hsaids_duplicates),
            'same_hash_count': len(same_hash_files),
            'duplicate_files': '; '.join(all_duplicate_files) if all_duplicate_files else '',
            'frequency': frequency,
            'detection_layer': layer,
            'file_size_bytes': file_size,
            'file_size_mb': round(file_size / (1024 * 1024), 2)
        })
    
    uniques_data = []
    for item in unique_files:
        if len(item) >= 5:
            file_path, file_hash, object_id, group_id, frequency = item[:5]
        else:
            file_path, file_hash = item[:2]
            object_id, group_id, frequency = None, None, 0
        
        try:
            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        except OSError:
            file_size = 0
        
        uniques_data.append({
            'file_path': file_path,
            'file_hash': file_hash,
            'object_id': object_id,
            'container_group_id': group_id,
            'frequency': frequency,
            'file_size_bytes': file_size,
            'file_size_mb': round(file_size / (1024 * 1024), 2)
        })
    
    # Save to CSV
    if duplicates_data:
        duplicates_df = pd.DataFrame(duplicates_data)
        duplicates_df.to_csv("duplicate_files.csv", index=False)
        print(f"\n💾 Saved duplicate files list to: duplicate_files.csv")
    
    if uniques_data:
        uniques_df = pd.DataFrame(uniques_data)
        uniques_df.to_csv("unique_files.csv", index=False)
        print(f"💾 Saved unique files list to: unique_files.csv")
    
    # Save duplicate groups (files grouped by hash)
    if duplicate_groups:
        groups_data = []
        for hash_val, paths in duplicate_groups.items():
            for path in paths:
                try:
                    file_size = os.path.getsize(path) if os.path.exists(path) else 0
                except OSError:
                    file_size = 0
                groups_data.append({
                    'file_hash': hash_val,
                    'file_path': path,
                    'group_size': len(paths),
                    'file_size_bytes': file_size,
                    'file_size_mb': round(file_size / (1024 * 1024), 2)
                })
        
        groups_df = pd.DataFrame(groups_data)
        groups_df.to_csv("duplicate_groups.csv", index=False)
        print(f"💾 Saved duplicate groups to: duplicate_groups.csv")
    
    # Save HSAIDS statistics
    if hsaids_stats:
        stats_df = pd.DataFrame([hsaids_stats])
        stats_df.to_csv("hsaids_statistics.csv", index=False)
        print(f"💾 Saved HSAIDS statistics to: hsaids_statistics.csv")
    
    print("\n✅ All results saved!")


def display_summary(duplicate_files, unique_files, duplicate_groups, all_files_count, hsaids_stats=None):
    """Display summary of duplicate files found with HSAIDS statistics."""
    print(f"\n📊 Summary:")
    print(f"   Total files scanned: {all_files_count}")
    print(f"   Unique files: {len(unique_files)}")
    print(f"   Duplicate file groups: {len(duplicate_groups)}")
    print(f"   Total duplicate files: {len(duplicate_files)}")
    
    if hsaids_stats:
        print(f"\n🔬 HSAIDS Performance Metrics:")
        print(f"   Bayesian Search Confidence: {hsaids_stats['bayesian_confidence']:.3f}")
        print(f"   Container Groups Created: {hsaids_stats['container_groups']}")
        print(f"   Adaptive Merge Frequency: {hsaids_stats['merge_frequency']}")
        print(f"   GC Efficiency: {hsaids_stats['gc_stats']['objects_reclaimed']} objects reclaimed")
    
    # Display duplicate groups
    if duplicate_groups:
        print(f"\n🔍 Duplicate File Groups (showing first 10):")
        for i, (hash_val, paths) in enumerate(list(duplicate_groups.items())[:10]):
            print(f"\n   Group {i+1} ({len(paths)} files with same content):")
            for path in paths:
                try:
                    file_size = os.path.getsize(path) if os.path.exists(path) else 0
                    size_mb = file_size / (1024 * 1024)
                    print(f"      - {path} ({size_mb:.2f} MB)")
                except OSError:
                    print(f"      - {path} (size unknown)")
        
        if len(duplicate_groups) > 10:
            print(f"\n   ... and {len(duplicate_groups) - 10} more groups")
    else:
        print("\n✅ No duplicate files found!")


def main():
    """Main function."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Find duplicate files using HSAIDS (Hierarchical Sketch Assisted Inline De-duplication Scheme)'
    )
    parser.add_argument(
        'path',
        nargs='?',
        default='.',
        help='Directory path to scan for duplicate files (default: current directory)'
    )
    parser.add_argument(
        '--include-hidden',
        action='store_true',
        help='Include hidden files and directories in the scan'
    )
    parser.add_argument(
        '--no-save',
        action='store_true',
        help='Do not save results to CSV files'
    )
    parser.add_argument(
        '--no-gc',
        action='store_true',
        help='Disable garbage collection during processing'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show detailed progress during directory scanning'
    )
    
    args = parser.parse_args()
    
    # Validate path
    if not os.path.isdir(args.path):
        print(f"❌ Error: '{args.path}' is not a valid directory")
        return
    
    # Find duplicates using HSAIDS
    duplicate_files, unique_files, duplicate_groups, hsaids_stats = find_duplicate_files(
        args.path, 
        include_hidden=args.include_hidden,
        enable_gc=not args.no_gc,
        verbose=args.verbose
    )
    
    # Display summary
    all_files_count = len(duplicate_files) + len(unique_files)
    display_summary(duplicate_files, unique_files, duplicate_groups, all_files_count, hsaids_stats)
    
    # Save results
    if not args.no_save:
        save_results(duplicate_files, unique_files, duplicate_groups, hsaids_stats)


if __name__ == "__main__":
    main()

