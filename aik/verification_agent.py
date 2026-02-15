from __future__ import annotations

"""
Verification-enabled agent.

All verification logic is implemented in aik/agent.py and controlled via AgentConfig.
This module exists as a stable import path for "verification agent" features.

Goal verification is now integrated directly into the main agent loop:
- Before marking a task as complete, the agent takes a verification screenshot
- Uses Claude Vision API to confirm the goal state is actually visible
- Only stops when (verified == True AND confidence >= threshold)
- Falls back to continuing with additional attempts on verification failure
- Gives up after max_stop_verification_failures to prevent infinite loops

Goal decomposition (for complex multi-step goals) is also built in:
- Automatically breaks complex goals into verifiable stages
- Each stage has its own verification criteria
- Stage progress is tracked and reported in the session summary
"""

from .agent import AgentConfig, KeyboardVisionAgent
from .goal_verifier import GoalVerifier, GoalVerificationResult
from .goal_decomposer import GoalDecomposer, GoalDecomposition, GoalStage
from .failure_detector import detect_failure, FailureSignals
from .recovery_strategies import suggest_recovery, RecoveryAdvice


VerificationAgentConfig = AgentConfig
VerificationKeyboardVisionAgent = KeyboardVisionAgent

__all__ = [
    "AgentConfig",
    "KeyboardVisionAgent",
    "VerificationAgentConfig",
    "VerificationKeyboardVisionAgent",
    "GoalVerifier",
    "GoalVerificationResult",
    "GoalDecomposer",
    "GoalDecomposition",
    "GoalStage",
    "FailureSignals",
    "RecoveryAdvice",
    "detect_failure",
    "suggest_recovery",
]
