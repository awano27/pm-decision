"""R11: the one line that says which question the model was unsure of, or which rule sent the item to a person.

It is worked out from the recorded path of a result (graph.run) and nothing else: no model is asked, nothing is written, and no
number is shown (a close pair of options becomes "ほぼ互角" or "やや差"). A node, an option or a playbook without a label gives
no line rather than an English one. notify.py shows the line in the approval post and in the detail when post_reason is 1."""

# the question of each judge and plan node of the bundled graphs (graphs/*.json), by (graph name, node id)
QUESTION_LABEL = {
    ("workitem-intake", "ready"): "受け入れ条件などが揃っているか",
    ("workitem-intake", "priority"): "優先度",
    ("minutes-followup", "item_type"): "議事の種類",
    ("minutes-followup", "has_owner"): "担当者が決まっているか",
    ("minutes-followup", "has_due"): "期限が決まっているか",
    ("minutes-followup", "plan_risk"): "次の進め方",
    ("alert-triage", "impact"): "お客様への影響の大きさ",
    ("alert-triage", "noise_check"): "ノイズかどうか",
    ("alert-triage", "future_risk"): "これから起きる問題か",
    ("alert-triage", "horizon"): "いつ起きそうか",
    ("alert-triage", "plan_incident"): "次の進め方",
    ("teams-chat-triage", "intent"): "依頼の種類",
    ("teams-chat-triage", "urgency"): "判断の期限",
    ("teams-chat-triage", "plan_decision"): "次の進め方",
    ("teams-chat-triage", "plan_requirements"): "次の進め方",
}

# the options of a choice question, by (graph name, node id): the option -> the word the line uses
OPTION_LABEL = {
    ("minutes-followup", "item_type"): {"decision": "決定事項", "action": "作業", "risk": "リスク", "info": "情報の共有"},
    ("teams-chat-triage", "intent"): {"decision": "判断・決定", "requirements": "要件整理", "status": "状況確認",
                                      "incident": "障害・不具合", "fyi": "共有・お礼", "none": "雑談・無関係"},
}

# the playbooks (playbooks/*.json) and "none" (the plan picked none of them)
PLAYBOOK_LABEL = {
    "incident": "障害対応", "release_readiness": "リリースの可否", "requirement_clarification": "要件の明確化",
    "requirements_review": "要件の確認", "risk_mitigation": "リスクへの対応", "schedule_change": "スケジュールの変更",
    "scope_change": "範囲の変更", "staffing": "体制・人員", "stakeholder_escalation": "関係者への報告・相談",
    "none": "当てはまる手順なし",
}

MIXED_LINE = "規則: 障害の語と、復旧・試験などの語が両方あり人へ"   # a rule read both ways (a recovery or a test word next to the report)
SEVERE_LINE = "重大度（Sev0/1・優先度 1）の規則のため人へ"        # the graph's "severe" field (monitor: severity; work item: priority)


def open_question(rec):
    """The one line ("" when there is none) that says why an item reached the PM by an uncertain route. A severity rule is named
    first; otherwise the last step of the path that was unsure or mixed. Old records without a path give no line."""
    if not isinstance(rec, dict):
        return ""
    if rec.get("severe_guard"):
        return SEVERE_LINE
    path = rec.get("path")
    if not isinstance(path, list):
        return ""
    for step in reversed(path):
        if not isinstance(step, dict):
            continue
        if step.get("edge") == "mixed":
            return MIXED_LINE
        if step.get("edge") == "unsure":
            return _unsure(rec.get("graph"), step)
    return ""


def _unsure(graph_name, step):
    node = step.get("node")
    label = QUESTION_LABEL.get((graph_name, node))
    answer = step.get("answer") if isinstance(step.get("answer"), dict) else {}
    if label is None:
        return ""
    if "invalid" in answer:
        why = "答えを読めませんでした"
    elif "guard" in answer:   # the score was under the field's severity: a person decides (graph.guard_hit)
        why = "元の重大度より低く採点したため人へ"
    elif "score" in answer:
        why = "中ほどで迷いました"
    else:
        why = _near(graph_name, node, answer) or "はっきりしませんでした"
    return f"迷った点: {label}（{why}）"


def _near(graph_name, node, answer):
    """The option the answer leaned to, in words: "近いのは 状況確認" (plus "ほぼ互角" or "やや差" for the runner-up). A plan keeps
    only the playbook it chose, so it names that one. "" when there is nothing to name."""
    if "playbook" in answer:
        name = answer.get("playbook")
        return f"近いのは {PLAYBOOK_LABEL[name]}" if isinstance(name, str) and name in PLAYBOOK_LABEL and name != "none" else ""
    labels = OPTION_LABEL.get((graph_name, node), {})
    ranked = _ranked(answer.get("probabilities"))
    if ranked:
        if ranked[0][1] not in labels:
            return ""
        tail = ""
        if len(ranked) > 1 and ranked[1][1] in labels:
            gap = ranked[0][0] - ranked[1][0]
            if gap < 0.1:
                tail = f"。次点の{labels[ranked[1][1]]}とほぼ互角"
            elif gap < 0.25:
                tail = f"。次点の{labels[ranked[1][1]]}とはやや差"
        return f"近いのは {labels[ranked[0][1]]}{tail}"
    choice = answer.get("choice")
    return f"近いのは {labels[choice]}" if isinstance(choice, str) and choice in labels else ""


def _ranked(probabilities):
    """[(probability, option)] best first. Only numbers count; the text of an option never does."""
    if not isinstance(probabilities, dict):
        return []
    out = [(float(p), str(k)) for k, p in probabilities.items() if isinstance(p, (int, float)) and not isinstance(p, bool)]
    return sorted(out, reverse=True)
