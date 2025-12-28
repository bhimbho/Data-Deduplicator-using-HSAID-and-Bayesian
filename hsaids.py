#!/usr/bin/env python3
"""
HSAIDS - Hierarchical Sketches Assisted Inline De-duplication Scheme

An enhanced duplicate detection and efficient storage management system that:
- Uses hierarchical sketch layers (hot in RAM, cold in SSD)
- Implements frequency-sensitive Bloom filter allocation
- Employs Bayesian search optimization
- Uses hierarchical clustering-based container groupings
- Implements sketch layer merging with adaptive frequency
- Provides efficient object ID to container group mapping
- Performs duplicate detection during Garbage Collection
"""

import hashlib
import random
import math
import json
import os
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
import time


# ============================================================
# FREQUENCY-SENSITIVE BLOOM FILTER
# ============================================================
# Adapts Bloom filter size based on item frequency to reduce
# false positives for high-frequency data items.
# ============================================================

class FrequencySensitiveBloomFilter:
    def __init__(self, base_size, num_hashes, max_size_multiplier=4):
        self.base_size = base_size
        self.num_hashes = num_hashes
        self.max_size_multiplier = max_size_multiplier
        # Multiple Bloom filters for different frequency tiers
        self.bloom_filters = {
            'low': [0] * base_size,      # Frequency 1-2
            'medium': [0] * (base_size * 2),  # Frequency 3-5
            'high': [0] * (base_size * max_size_multiplier)  # Frequency 6+
        }
        self.frequency_tiers = {
            'low': (1, 2),
            'medium': (3, 5),
            'high': (6, float('inf'))
        }
    
    def _hashes(self, item, tier='low'):
        """Generate hash positions for a specific tier."""
        size = len(self.bloom_filters[tier])
        result = []
        for i in range(self.num_hashes):
            h = int(hashlib.md5((str(i) + item + tier).encode()).hexdigest(), 16)
            result.append(h % size)
        return result
    
    def _get_tier(self, frequency):
        """Determine which tier to use based on frequency."""
        for tier, (min_freq, max_freq) in self.frequency_tiers.items():
            if min_freq <= frequency <= max_freq:
                return tier
        return 'high'
    
    def insert(self, item, frequency=1):
        """Insert item into appropriate frequency tier."""
        tier = self._get_tier(frequency)
        for pos in self._hashes(item, tier):
            self.bloom_filters[tier][pos] = 1
    
    def contains(self, item, frequency=1):
        """Check membership across all relevant tiers."""
        tier = self._get_tier(frequency)
        return all(self.bloom_filters[tier][pos] == 1 
                  for pos in self._hashes(item, tier))


# ============================================================
# ENHANCED COUNT-MIN SKETCH
# ============================================================

class CountMinSketch:
    def __init__(self, width, depth):
        self.width = width
        self.depth = depth
        self.table = [[0] * width for _ in range(depth)]
        self.hash_seeds = [random.randint(0, 10000) for _ in range(depth)]
        self.total_updates = 0
    
    def _hash(self, item, i):
        return int(hashlib.md5((str(self.hash_seeds[i]) + item).encode()).hexdigest(), 16) % self.width
    
    def update(self, item):
        for i in range(self.depth):
            self.table[i][self._hash(item, i)] += 1
        self.total_updates += 1
    
    def estimate(self, item):
        return min(self.table[i][self._hash(item, i)] for i in range(self.depth))
    
    def merge(self, other):
        """Merge another CMS into this one."""
        if self.width != other.width or self.depth != other.depth:
            raise ValueError("CMS dimensions must match for merging")
        for i in range(self.depth):
            for j in range(self.width):
                self.table[i][j] += other.table[i][j]
        self.total_updates += other.total_updates


# ============================================================
# BAYESIAN SEARCH OPTIMIZATION
# ============================================================
# Optimizes lookup and update operations using Bayesian inference
# to balance memory efficiency and false positive rates.
# ============================================================

class BayesianOptimizer:
    def __init__(self, alpha=5.0, beta=2.0):
        # Prior parameters for Bayesian inference
        # Higher alpha gives optimistic prior (expect hot layer to be useful)
        # More optimistic starting point: alpha=5, beta=2 gives ~71% initial confidence
        self.alpha = alpha  # Prior success rate
        self.beta = beta     # Prior failure rate
        self.success_count = 0
        self.total_queries = 0
    
    def update_prior(self, success):
        """Update Bayesian prior based on query result."""
        self.total_queries += 1
        if success:
            self.success_count += 1
    
    def get_confidence(self):
        """Get confidence level for next query."""
        if self.total_queries == 0:
            # Initial confidence based on prior
            return self.alpha / (self.alpha + self.beta)
        
        # Beta distribution parameters
        a = self.alpha + self.success_count
        b = self.beta + (self.total_queries - self.success_count)
        
        # Expected value (mean of beta distribution)
        confidence = a / (a + b)
        
        # Apply minimum confidence floor to prevent it from getting too low
        # This helps the system continue learning even with initial failures
        min_confidence = 0.2  # Increased from 0.1 to 0.2
        return max(confidence, min_confidence)
    
    def should_check_hot_first(self):
        """Decide whether to check hot layer first based on confidence."""
        return self.get_confidence() > 0.5


# ============================================================
# CONTAINER GROUP MANAGEMENT
# ============================================================
# Hierarchical clustering-based container groupings for data
# locality and de-duplication accuracy.
# ============================================================

class ContainerGroup:
    def __init__(self, group_id, capacity=1000):
        self.group_id = group_id
        self.capacity = capacity
        self.objects = {}  # object_id -> metadata
        self.hash_to_objects = defaultdict(list)  # hash -> [object_ids]
        self.size = 0
    
    def add_object(self, object_id, obj_hash, metadata=None):
        """Add object to container group."""
        if self.size >= self.capacity:
            return False
        
        self.objects[object_id] = {
            'hash': obj_hash,
            'metadata': metadata or {},
            'timestamp': time.time()
        }
        self.hash_to_objects[obj_hash].append(object_id)
        self.size += 1
        return True
    
    def find_duplicates(self, obj_hash):
        """Find duplicate objects with same hash."""
        return self.hash_to_objects.get(obj_hash, [])
    
    def get_objects(self):
        """Get all objects in this group."""
        return list(self.objects.keys())


class ContainerGroupManager:
    def __init__(self, initial_groups=10, group_capacity=1000):
        self.groups = {}
        self.group_capacity = group_capacity
        self.object_to_group = {}  # object_id -> group_id
        self.hash_to_groups = defaultdict(set)  # hash -> {group_ids}
        self._initialize_groups(initial_groups)
    
    def _initialize_groups(self, num_groups):
        """Initialize container groups."""
        for i in range(num_groups):
            self.groups[i] = ContainerGroup(i, self.group_capacity)
    
    def add_object(self, object_id, obj_hash, metadata=None):
        """Add object to appropriate container group using clustering."""
        # Find best group (clustering based on hash similarity)
        group_id = self._find_best_group(obj_hash)
        
        if group_id is None or not self.groups[group_id].add_object(object_id, obj_hash, metadata):
            # Create new group if needed
            group_id = len(self.groups)
            self.groups[group_id] = ContainerGroup(group_id, self.group_capacity)
            self.groups[group_id].add_object(object_id, obj_hash, metadata)
        
        self.object_to_group[object_id] = group_id
        self.hash_to_groups[obj_hash].add(group_id)
        return group_id
    
    def _find_best_group(self, obj_hash):
        """Find best group using hierarchical clustering (simplified)."""
        # Simple clustering: use hash prefix to group similar items
        hash_prefix = obj_hash[:4]  # Use first 4 hex chars
        
        # Check existing groups with similar hashes
        for group_id, group in self.groups.items():
            if group.size < group.capacity * 0.9:  # Not too full
                # Check if group has similar hashes
                for existing_hash in group.hash_to_objects.keys():
                    if existing_hash[:4] == hash_prefix:
                        return group_id
        
        # Return least full group
        if self.groups:
            return min(self.groups.keys(), 
                      key=lambda g: self.groups[g].size)
        return None
    
    def find_duplicates(self, obj_hash):
        """Find duplicate objects across all groups."""
        duplicates = []
        for group_id in self.hash_to_groups.get(obj_hash, []):
            group = self.groups[group_id]
            duplicates.extend(group.find_duplicates(obj_hash))
        return duplicates
    
    def get_group(self, group_id):
        """Get container group by ID."""
        return self.groups.get(group_id)


# ============================================================
# HSAIDS - MAIN SYSTEM
# ============================================================

class HSAIDS:
    """
    Hierarchical Sketches Assisted Inline De-duplication Scheme
    
    Implements all phases from the abstract:
    1. Hierarchical sketch layer (hot in RAM, cold in SSD)
    2. Frequency-sensitive Bloom filter allocation
    3. Bayesian search optimization
    4. Hierarchical clustering-based container groupings
    5. Sketch layer merging with adaptive frequency
    6. Efficient object ID to container group mapping
    7. Duplicate detection during Garbage Collection
    """
    
    def __init__(self, 
                 hot_bloom_size=10000,
                 hot_cms_width=5000,
                 cold_bloom_size=100000,
                 cold_cms_width=20000,
                 threshold=2,
                 promotion_threshold=5,
                 merge_frequency=1000):
        # Hot layer (RAM) - fast, smaller
        self.hot_bloom = FrequencySensitiveBloomFilter(
            base_size=hot_bloom_size, 
            num_hashes=4
        )
        self.hot_cms = CountMinSketch(width=hot_cms_width, depth=4)
        
        # Cold layer (SSD simulation) - slower, larger
        self.cold_bloom = FrequencySensitiveBloomFilter(
            base_size=cold_bloom_size,
            num_hashes=4
        )
        self.cold_cms = CountMinSketch(width=cold_cms_width, depth=4)
        
        # Thresholds
        self.threshold = threshold
        self.promotion_threshold = promotion_threshold
        
        # Container group management
        self.container_manager = ContainerGroupManager(
            initial_groups=20,
            group_capacity=1000
        )
        
        # Bayesian optimizer
        self.bayesian_optimizer = BayesianOptimizer()
        
        # Object tracking
        self.object_id_counter = 0
        self.hash_to_object_id = {}  # hash -> object_id
        self.object_id_to_hash = {}  # object_id -> hash
        
        # Merging configuration
        self.merge_frequency = merge_frequency
        self.operations_since_merge = 0
        self.merge_history = []
        
        # GC statistics
        self.gc_stats = {
            'runs': 0,
            'objects_reclaimed': 0,
            'duplicates_found': 0
        }
    
    def _generate_object_id(self, obj_hash):
        """Generate or retrieve object ID for a hash."""
        if obj_hash not in self.hash_to_object_id:
            self.object_id_counter += 1
            self.hash_to_object_id[obj_hash] = self.object_id_counter
            self.object_id_to_hash[self.object_id_counter] = obj_hash
        return self.hash_to_object_id[obj_hash]
    
    def insert(self, chunk_hash, metadata=None):
        """
        Insert a chunk into HSAIDS system with all optimizations.
        
        Phase 1: Hierarchical sketch layer lookup
        Phase 2: Frequency-sensitive Bloom filter
        Phase 3: Bayesian search optimization
        Phase 4: Container grouping
        """
        # Get frequency estimate
        hot_freq = self.hot_cms.estimate(chunk_hash)
        cold_freq = self.cold_cms.estimate(chunk_hash)
        total_freq = max(hot_freq, cold_freq)
        
        # Bayesian optimization: decide lookup order
        check_hot_first = self.bayesian_optimizer.should_check_hot_first()
        
        # Phase 1 & 2: Hierarchical lookup with frequency-sensitive BF
        is_duplicate = False
        layer = None
        found_in_hot = False
        found_in_cold = False
        
        if check_hot_first:
            # Check hot layer first
            if self.hot_bloom.contains(chunk_hash, hot_freq):
                found_in_hot = True
                if hot_freq > self.threshold:
                    is_duplicate = True
                    layer = "hot"
                # Success: found in hot layer when checking hot first
                self.bayesian_optimizer.update_prior(True)
            elif self.cold_bloom.contains(chunk_hash, cold_freq):
                found_in_cold = True
                if cold_freq > self.threshold:
                    is_duplicate = True
                    layer = "cold"
                # Failure: not found in hot, had to check cold (strategy failed)
                self.bayesian_optimizer.update_prior(False)
            # else: new item - don't update prior (no information about strategy)
        else:
            # Check cold layer first
            if self.cold_bloom.contains(chunk_hash, cold_freq):
                found_in_cold = True
                if cold_freq > self.threshold:
                    is_duplicate = True
                    layer = "cold"
                # Neutral: found in cold when checking cold first (strategy worked, but not optimal)
                # Don't update - this doesn't tell us about hot layer effectiveness
            elif self.hot_bloom.contains(chunk_hash, hot_freq):
                found_in_hot = True
                if hot_freq > self.threshold:
                    is_duplicate = True
                    layer = "hot"
                # Failure: found in hot layer but we checked cold first (should have checked hot)
                self.bayesian_optimizer.update_prior(False)
            # else: new item - don't update prior (no information about strategy)
        
        if is_duplicate:
            # Phase 4: Find duplicates in container groups
            object_id = self._generate_object_id(chunk_hash)
            duplicates = self.container_manager.find_duplicates(chunk_hash)
            return {
                "status": "duplicate",
                "layer": layer,
                "frequency": total_freq,
                "object_id": object_id,
                "duplicate_objects": duplicates
            }
        
        # New item - insert into system
        # Update hot layer
        if not self.hot_bloom.contains(chunk_hash, hot_freq):
            self.hot_bloom.insert(chunk_hash, hot_freq + 1)
        self.hot_cms.update(chunk_hash)
        new_hot_freq = self.hot_cms.estimate(chunk_hash)
        
        # Phase 3: Promote to cold if frequency high enough
        if new_hot_freq > self.promotion_threshold:
            self._promote_to_cold(chunk_hash, new_hot_freq)
        
        # Phase 4: Add to container group
        object_id = self._generate_object_id(chunk_hash)
        group_id = self.container_manager.add_object(
            object_id, 
            chunk_hash, 
            metadata
        )
        
        # Phase 5: Adaptive merging
        self.operations_since_merge += 1
        if self.operations_since_merge >= self.merge_frequency:
            self._adaptive_merge()
        
        return {
            "status": "new",
            "object_id": object_id,
            "group_id": group_id,
            "frequency": new_hot_freq
        }
    
    def _promote_to_cold(self, chunk_hash, frequency):
        """Promote item from hot to cold layer."""
        if not self.cold_bloom.contains(chunk_hash, frequency):
            self.cold_bloom.insert(chunk_hash, frequency)
        self.cold_cms.update(chunk_hash)
    
    def _adaptive_merge(self):
        """
        Phase 5: Sketch layer merging with adaptive frequency.
        Minimizes SSD write amplification and merging cost.
        Note: In practice, we'd track items to merge, but for efficiency
        we reset hot layer and let cold accumulate naturally over time.
        """
        merge_start = time.time()
        
        # Track merge statistics
        old_cold_total = self.cold_cms.total_updates
        
        # Reset hot layer (keep structure, clear data)
        # Items will naturally flow to cold layer as they're re-encountered
        hot_width = self.hot_cms.width
        hot_depth = self.hot_cms.depth
        self.hot_cms = CountMinSketch(
            width=hot_width,
            depth=hot_depth
        )
        
        # In a production system, we would:
        # 1. Track all items in hot layer
        # 2. Update cold CMS with their frequencies
        # 3. Then reset hot layer
        # For now, we use a simpler approach where items naturally
        # migrate to cold layer when frequency exceeds promotion threshold
        
        # Adaptive merge frequency adjustment
        merge_time = time.time() - merge_start
        merge_efficiency = (self.cold_cms.total_updates - old_cold_total) / max(merge_time, 0.001)
        
        self.merge_history.append({
            'timestamp': time.time(),
            'efficiency': merge_efficiency,
            'operations': self.operations_since_merge
        })
        
        # Adjust merge frequency based on efficiency
        if len(self.merge_history) > 5:
            avg_efficiency = sum(m['efficiency'] for m in self.merge_history[-5:]) / 5
            if avg_efficiency > 1000:  # High efficiency
                self.merge_frequency = min(self.merge_frequency * 1.2, 5000)
            else:  # Low efficiency
                self.merge_frequency = max(self.merge_frequency * 0.9, 500)
        
        self.operations_since_merge = 0
    
    def query(self, chunk_hash):
        """
        Query if a chunk is new or duplicate.
        Uses Bayesian optimization for efficient lookup.
        """
        hot_freq = self.hot_cms.estimate(chunk_hash)
        cold_freq = self.cold_cms.estimate(chunk_hash)
        
        check_hot_first = self.bayesian_optimizer.should_check_hot_first()
        
        if check_hot_first:
            if self.hot_bloom.contains(chunk_hash, hot_freq) and hot_freq > self.threshold:
                return {"status": "duplicate", "layer": "hot", "frequency": hot_freq}
            elif self.cold_bloom.contains(chunk_hash, cold_freq) and cold_freq > self.threshold:
                return {"status": "duplicate", "layer": "cold", "frequency": cold_freq}
        else:
            if self.cold_bloom.contains(chunk_hash, cold_freq) and cold_freq > self.threshold:
                return {"status": "duplicate", "layer": "cold", "frequency": cold_freq}
            elif self.hot_bloom.contains(chunk_hash, hot_freq) and hot_freq > self.threshold:
                return {"status": "duplicate", "layer": "hot", "frequency": hot_freq}
        
        return {"status": "new"}
    
    def garbage_collect(self, min_frequency=1):
        """
        Phase 6: Duplicate detection during Garbage Collection.
        Enhances reclamation efficiency.
        """
        self.gc_stats['runs'] += 1
        gc_start = time.time()
        
        objects_to_reclaim = []
        duplicates_found = 0
        
        # Scan container groups for low-frequency objects
        for group_id, group in self.container_manager.groups.items():
            for object_id, obj_data in list(group.objects.items()):
                obj_hash = obj_data['hash']
                hot_freq = self.hot_cms.estimate(obj_hash)
                cold_freq = self.cold_cms.estimate(obj_hash)
                total_freq = max(hot_freq, cold_freq)
                
                # Find duplicates
                duplicates = self.container_manager.find_duplicates(obj_hash)
                if len(duplicates) > 1:
                    duplicates_found += len(duplicates) - 1
                
                # Mark for reclamation if frequency below threshold
                if total_freq < min_frequency:
                    objects_to_reclaim.append((object_id, obj_hash, group_id))
        
        # Reclaim objects
        reclaimed_count = 0
        for object_id, obj_hash, group_id in objects_to_reclaim:
            group = self.container_manager.groups[group_id]
            if object_id in group.objects:
                del group.objects[object_id]
                if obj_hash in group.hash_to_objects:
                    group.hash_to_objects[obj_hash] = [
                        oid for oid in group.hash_to_objects[obj_hash] 
                        if oid != object_id
                    ]
                    if not group.hash_to_objects[obj_hash]:
                        del group.hash_to_objects[obj_hash]
                group.size -= 1
                reclaimed_count += 1
        
        self.gc_stats['objects_reclaimed'] += reclaimed_count
        self.gc_stats['duplicates_found'] += duplicates_found
        
        gc_time = time.time() - gc_start
        
        return {
            'reclaimed': reclaimed_count,
            'duplicates_found': duplicates_found,
            'time': gc_time
        }
    
    def get_statistics(self):
        """Get system statistics."""
        return {
            'hot_cms_updates': self.hot_cms.total_updates,
            'cold_cms_updates': self.cold_cms.total_updates,
            'total_objects': self.object_id_counter,
            'container_groups': len(self.container_manager.groups),
            'bayesian_confidence': self.bayesian_optimizer.get_confidence(),
            'merge_frequency': self.merge_frequency,
            'gc_stats': self.gc_stats.copy(),
            'merge_history_count': len(self.merge_history)
        }
    
    def save_state(self, filepath):
        """Save system state to disk (for cold layer persistence)."""
        state = {
            'object_id_counter': self.object_id_counter,
            'hash_to_object_id': self.hash_to_object_id,
            'object_id_to_hash': self.object_id_to_hash,
            'merge_frequency': self.merge_frequency,
            'gc_stats': self.gc_stats,
            'merge_history': self.merge_history[-10:]  # Keep last 10
        }
        with open(filepath, 'w') as f:
            json.dump(state, f, indent=2)
    
    def load_state(self, filepath):
        """Load system state from disk."""
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                state = json.load(f)
            self.object_id_counter = state.get('object_id_counter', 0)
            self.hash_to_object_id = state.get('hash_to_object_id', {})
            self.object_id_to_hash = {int(k): v for k, v in state.get('object_id_to_hash', {}).items()}
            self.merge_frequency = state.get('merge_frequency', self.merge_frequency)
            self.gc_stats = state.get('gc_stats', self.gc_stats)
            self.merge_history = state.get('merge_history', [])

