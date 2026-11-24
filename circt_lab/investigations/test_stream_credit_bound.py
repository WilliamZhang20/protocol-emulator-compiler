#!/usr/bin/env python3
"""Check sizing boundaries and overloaded service cases."""
from stream_credit_bound import assess

assert assess(4, 16, 1, 3, 1)['required_depth'] == 2
assert assess(16, 16, 1, 3, 1)['required_depth'] == 2
assert assess(17, 16, 1, 3, 1)['required_depth'] == 3
assert assess(4, 16, 1, 3, 1, 12)['required_depth'] == 2
assert assess(4, 16, 1, 3, 1, 13)['required_depth'] == 3
assert assess(4, 16, 4, 3, 1)['required_depth'] == 4
assert not assess(4, 2, 1, 3, 1)['sustained_rate_guaranteed']
assert assess(4, 3, 1, 3, 1)['sustained_rate_guaranteed']
assert not assess(4, 16, 1, 3, 1)['claims_deadlock_freedom']
print('PASS: FIFO depth, burst and sustained-service boundaries')
