# ADR-005 Enhancement Summary: Grouped & Tiered Criteria System

**Date:** 2026-02-01
**Enhancement:** Added support for grouped multi-value criteria and tiered preference matching
**Rationale:** Enable complex job matching scenarios like "department = sales OR marketing" and "geo = Latin America preferred, US East acceptable, On-site undesirable"

---

## What Was Added

### 1. Database Schema Enhancements

**New Tables:**

```sql
-- Grouped criteria (multi-value OR logic and tiered preferences)
criterion_groups (id, name, match_type, weight, active, created_date, notes)
--   match_type: 'any' (OR), 'all' (AND), 'tiered' (preferences)

-- Values within criterion groups
criterion_group_values (id, group_id, value, tier_level, tier_weight, created_date)
--   tier_level: 'preferred', 'acceptable', 'undesirable' (for tiered groups)
--   tier_weight: Override weight for specific tier (NULL uses group weight)
```

**Design Pattern:**
- Individual criteria (`search_criteria`) still supported for simple cases
- Grouped criteria (`criterion_groups` + `criterion_group_values`) for complex matching
- Both systems work together in scoring algorithm

---

## 2. Match Types Supported

### Match Type: 'any' (Multi-Value OR Logic)

**Use Case:** Flexible department/tech stack matching

**Example:**
```yaml
Group: "departments" (match_type='any', weight=15)
  Values:
    - sales
    - marketing
    - business development
```

**Scoring:** If job mentions ANY of these → +15 points (applied ONCE, not per match)

**Problem Solved:** Prevents double-counting when job mentions "sales and marketing"

---

### Match Type: 'tiered' (Preference Levels)

**Use Case:** Nuanced location/role preferences

**Example:**
```yaml
Group: "geography" (match_type='tiered')
  Tier: preferred (+20 points)
    - Latin America
    - Remote
  Tier: acceptable (0 points, neutral)
    - US East
    - US West
  Tier: undesirable (-10 points, penalty)
    - On-site only
```

**Scoring:** Checks tiers in order, uses first match
- Job in "Latin America" → +20 points (preferred)
- Job in "US East" → 0 points (acceptable, no penalty)
- Job "On-site only" → -10 points (undesirable)

**Problem Solved:** Expresses "preferred > acceptable > undesirable" hierarchies with neutral tier support

---

### Match Type: 'all' (Multi-Value AND Logic)

**Use Case:** Required combinations

**Example:**
```yaml
Group: "required_combo" (match_type='all', weight=25)
  Values:
    - remote
    - async
    - distributed team
```

**Scoring:** Must match ALL values → +25 points (for companies emphasizing distributed work culture)

---

## 3. Enhanced Scoring Algorithm

**Old Algorithm (Individual Criteria Only):**
```python
def score_job(job_data, active_criteria):
    score = 0
    for criterion in active_criteria:
        if criterion matches job:
            score += criterion.weight
    return score
```

**New Algorithm (Individual + Grouped Criteria):**
```python
def score_job(job_data, active_criteria, active_groups):
    score = 0

    # Phase 1: Individual criteria (existing)
    for criterion in active_criteria:
        if criterion matches job:
            score += criterion.weight

    # Phase 2: Grouped criteria (new)
    for group in active_groups:
        if group.match_type == 'any':
            if any value matches:
                score += group.weight  # Add ONCE

        elif group.match_type == 'tiered':
            for tier in [preferred, acceptable, undesirable]:
                if any value in tier matches:
                    score += tier.weight  # Use tier weight
                    break  # Stop at first matching tier

        elif group.match_type == 'all':
            if all values match:
                score += group.weight

    return min(score, 100)
```

---

## 4. Configuration Enhancements

**Added to `jobhunter-service.yaml`:**

```yaml
config:
  # Default criterion groups (Phase 3 - multi-value and tiered matching)
  default_criterion_groups:
    # Example 1: Multi-value OR matching
    - name: "departments"
      match_type: "any"
      weight: 15
      values:
        - "sales"
        - "marketing"
        - "business development"

    # Example 2: Tiered geography preferences
    - name: "geography"
      match_type: "tiered"
      values:
        - {value: "Latin America", tier: "preferred", weight: 20}
        - {value: "Remote", tier: "preferred", weight: 20}
        - {value: "US East", tier: "acceptable", weight: 0}
        - {value: "US West", tier: "acceptable", weight: 0}
        - {value: "On-site only", tier: "undesirable", weight: -10}

    # Example 3: Tech stack flexibility
    - name: "tech_stack"
      match_type: "any"
      weight: 10
      values: ["FastAPI", "Django", "Flask", "Tornado"]
```

---

## 5. New Database Operations

**JobHunterDatabaseManager - 12 New Methods:**

```python
# Group Management
def add_criterion_group(name, match_type, weight, active, notes) -> int
def get_active_groups() -> List[Dict]  # Groups with nested values
def get_group_by_id(id) -> Dict
def get_group_by_name(name) -> Dict
def update_criterion_group(id, name, match_type, weight, active) -> bool
def remove_criterion_group(id) -> bool  # Cascade deletes values

# Group Value Management
def add_group_value(group_id, value, tier_level, tier_weight) -> int
def get_group_values(group_id) -> List[Dict]
def update_group_value(value_id, value, tier_level, tier_weight) -> bool
def remove_group_value(value_id) -> bool

# Utilities
def get_groups_summary() -> Dict  # Formatted for display
def initialize_default_groups(default_list: List[Dict])
```

---

## 6. New Command Interface

**JobHunterService - 5 New Command Handlers:**

```python
# Create and manage criterion groups
_handle_add_group(sender, args, correlation_id)
_handle_add_group_value(sender, args, correlation_id)
_handle_list_groups(sender, correlation_id)
_handle_remove_group(sender, args, correlation_id)
_handle_remove_group_value(sender, args, correlation_id)

# Enhanced test command (now includes groups)
_handle_test_criteria(sender, args, correlation_id)  # Tests individual + grouped
```

**Command Examples:**

```bash
# Create multi-value OR group
@jobhunter-service add-group departments any 15
@jobhunter-service add-group-value departments sales
@jobhunter-service add-group-value departments marketing

# Create tiered geography group
@jobhunter-service add-group geography tiered
@jobhunter-service add-group-value geography "Latin America" preferred 20
@jobhunter-service add-group-value geography Remote preferred 20
@jobhunter-service add-group-value geography US_East acceptable 0
@jobhunter-service add-group-value geography On-site_only undesirable -10

# List all groups with values
@jobhunter-service list-groups

# Test scoring with sample job text
@jobhunter-service test-criteria "Senior Sales Engineer - Remote, Latin America - $150k"
# Output:
#   Individual Score: 45/100
#   Grouped Score: +45 pts (departments:sales +15, geography:Latin America +20, ...)
#   Total: 90/100 → HIGH PRIORITY MATCH
```

---

## 7. Enhanced Phase 3 Implementation Plan

**Old Plan:** 2-3 hours
**New Plan:** 3-4 hours

**Part A: Individual Criteria (1.5 hours)**
- Database operations for simple criteria
- Command handlers (set/list/remove)
- Validation and testing

**Part B: Grouped Criteria (1.5-2 hours)**
- Database operations for groups and values
- Command handlers (add-group, add-group-value, list-groups, etc.)
- Support for 'any', 'all', 'tiered' match types
- Tier level support (preferred/acceptable/undesirable)
- Enhanced test command with group scoring breakdown

---

## 8. Example Scoring Scenarios

### Scenario A: Job with Grouped Criteria Match

```
Job: "Senior Sales Engineer - Remote, Latin America - $150k - FastAPI backend"

Individual Criteria:
✓ keyword: senior_engineer (+15)
✓ min_salary: $150k >= $120k (+20)
✓ required_skill: python (+10)
Individual Score: 45/100

Grouped Criteria:
✓ departments (any): matched "sales" → +15 pts
✓ geography (tiered): matched "Latin America" (preferred) → +20 pts
✓ tech_stack (any): matched "FastAPI" → +10 pts
Grouped Score: +45 pts

Total Score: 90/100 → 🔥 HIGH PRIORITY MATCH
```

### Scenario B: Acceptable Tier (Neutral, No Penalty)

```
Job: "Senior Python Engineer - US East (hybrid) - $150k"

Individual Criteria:
✓ keyword: python (+10)
✓ keyword: senior_engineer (+15)
✓ min_salary: $150k >= $120k (+20)
Individual Score: 45/100

Grouped Criteria:
✓ geography (tiered): matched "US East" (acceptable) → +0 pts (neutral)
✗ departments: no match (0 pts)
Grouped Score: +0 pts

Total Score: 45/100 → SAVED (below 60 threshold, no notification)
```

### Scenario C: Undesirable Tier (Penalty)

```
Job: "Senior Python Engineer - On-site only (Austin, TX) - $130k"

Individual Criteria:
✓ keyword: python (+10)
✓ keyword: senior_engineer (+15)
✓ min_salary: $130k >= $120k (+20)
Individual Score: 45/100

Grouped Criteria:
✓ geography (tiered): matched "On-site only" (undesirable) → -10 pts (penalty)
Grouped Score: -10 pts

Total Score: 35/100 → SAVED (low score, likely not a good fit)
```

---

## 9. Key Benefits

**Individual Criteria (Existing):**
- ✅ User control - change weights without code changes
- ✅ Personalization - different users, different criteria
- ✅ Transparency - see exactly why jobs matched

**Grouped Criteria (NEW):**
- ✅ **Multi-Value Flexibility**: OR logic for departments/tech stacks
- ✅ **Tiered Preferences**: "preferred > acceptable > undesirable" hierarchies
- ✅ **Neutral Scoring**: "No bonus, no penalty" for acceptable options
- ✅ **Geographical Hierarchies**: Model real-world location preferences
- ✅ **Reduced Complexity**: Group related values logically
- ✅ **Penalty Support**: Negative weights for undesirable attributes
- ✅ **Avoids Double-Counting**: OR logic adds weight ONCE, not per match

---

## 10. Timeline Impact

**Original ADR-005 Timeline:** 24-30 hours across 11 phases

**Updated Timeline:**
- Phase 3 increased from 2-3 hours → **3-4 hours** (+1 hour)
- **New Total: 25-31 hours**

The additional hour is for implementing grouped criteria database operations, command handlers, and testing the tiered matching logic.

---

## 11. Testing Additions

**New Test Cases for Phase 3:**

```python
# Grouped criteria database tests
test_add_criterion_group()
test_add_group_value()
test_get_active_groups()
test_remove_criterion_group_cascades_values()
test_tiered_values_ordered_correctly()

# Grouped criteria command tests
test_add_group_command()
test_add_group_value_command()
test_list_groups_command()
test_remove_group_command()

# Scoring algorithm tests with groups
test_any_match_type_or_logic()
test_tiered_match_type_preferred_tier()
test_tiered_match_type_acceptable_tier_neutral()
test_tiered_match_type_undesirable_tier_penalty()
test_all_match_type_and_logic()
test_combined_individual_and_grouped_scoring()
test_avoid_double_counting_with_any_groups()
```

---

## 12. Migration Path

**Backward Compatibility:**
- ✅ Existing `search_criteria` table unchanged
- ✅ Individual criteria still work exactly as before
- ✅ Grouped criteria are OPTIONAL - can use individual only
- ✅ Both systems can coexist

**Migration Strategy:**
1. Phase 1 & 2 complete → users have working individual criteria
2. Phase 3 implementation → add group tables, leave existing data
3. Users can gradually migrate to groups OR keep individual criteria
4. `initialize_default_groups()` only runs if database is empty

**No Breaking Changes:**
- Old commands still work: `set-criteria`, `list-criteria`, `remove-criteria`
- New commands added: `add-group`, `add-group-value`, `list-groups`
- Scoring algorithm handles both individual and grouped criteria

---

## Summary

This enhancement transforms the JobHunter service from a simple weighted keyword matcher into a sophisticated preference modeling system. Users can now express complex job search requirements like:

- "I prefer sales or marketing roles" (OR logic, avoid double-counting)
- "Remote in Latin America is ideal, but US East is acceptable, and I'll never take on-site" (tiered preferences with neutral and penalty tiers)
- "Must have Python AND FastAPI AND distributed team experience" (AND logic for combinations)

The design maintains backward compatibility while adding powerful new capabilities that solve real-world job search complexity.
