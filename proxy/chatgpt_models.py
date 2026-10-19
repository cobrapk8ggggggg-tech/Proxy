"""Account-provided lanes distinguish normal ChatGPT, Work mode and Research."""


def model_catalog(raw):
    lanes = {
        slug: c.get("model_lane")
        for c in raw.get("categories", [])
        for slug in c.get("supported_models", [])
    }
    result = []
    for model in raw.get("models", []):
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            continue
        slug = model["slug"]
        mode = (
            "work"
            if model.get("is_work_mode_model") is True
            else "research" if slug == "research" else "chat"
        )
        lane = lanes.get(slug) or {
            "auto": "auto",
            "reasoning": "thinking",
            "none": "instant",
        }.get(model.get("reasoning_type"), "standard")
        efforts = model.get(
            "supported_reasoning_efforts", model.get("supported_thinking_efforts", [])
        ) or [
            e.get("thinking_effort")
            for e in model.get("thinking_efforts", [])
            if isinstance(e, dict)
        ]
        efforts = [e for e in efforts if isinstance(e, str)]
        title = model.get("title") or slug
        label = (
            "وضع العمل"
            if mode == "work"
            else (
                "بحث معمّق"
                if mode == "research"
                else {
                    "auto": "Auto",
                    "instant": "Instant",
                    "thinking": "Thinking",
                    "thinking_mini": "Thinking mini",
                }.get(lane, "دردشة")
            )
        )
        result.append(
            {
                "id": slug,
                "upstream_id": slug,
                "title": title,
                "name": title + " · " + label,
                "mode": mode,
                "lane": lane,
                "is_work_mode_model": mode == "work",
                "reasoning_efforts": efforts,
                "reasoning_type": model.get("reasoning_type"),
                "description": model.get("description", ""),
            }
        )
    if not result:
        return [], None
    default = next(
        (
            m
            for m in result
            if m["id"] == raw.get("default_model_slug") and m["mode"] == "chat"
        ),
        None,
    )
    auto = (
        default
        if default and default["lane"] == "auto"
        else next(
            (m for m in result if m["mode"] == "chat" and m["lane"] == "auto"), None
        )
    )
    upstream = auto["id"] if auto else "auto"
    result = [m for m in result if m["id"] != "auto"]
    result.insert(
        0,
        {
            "id": "auto",
            "upstream_id": "auto",
            "title": "Auto",
            "name": "Auto · اختيار تلقائي للدردشة العادية",
            "mode": "chat",
            "lane": "auto",
            "is_work_mode_model": False,
            "reasoning_efforts": [],
            "description": "يختار ChatGPT نموذج الدردشة تلقائيًا، دون تشغيل وضع العمل.",
        },
    )
    return result, upstream


def select_model(catalog, settings):
    models = catalog.get("models", [])
    requested = settings.get("model") or "auto"
    selected = next((m for m in models if m["id"] == requested), None)
    mode = settings.get("chatgpt_mode") or (selected or {}).get("mode") or "chat"
    if mode not in ("chat", "work", "research"):
        raise ValueError("اختر نوع محادثة ChatGPT صالحًا.")
    if requested == "auto" and mode != "chat":
        selected = next((m for m in models if m.get("mode") == mode), None)
        if not selected:
            raise ValueError("لا يعرض الحساب نموذجًا متاحًا لهذا الوضع.")
    if selected and selected.get("mode", mode) != mode:
        raise ValueError(
            "النموذج لا يطابق نوع المحادثة. اختر نموذج دردشة أو غيّر الوضع صراحة."
        )
    if requested != "auto" and models and not selected:
        raise ValueError("النموذج المحدد غير متاح في الحساب. حدّث قائمة النماذج.")
    upstream = (
        (selected or {}).get("upstream_id") or (selected or {}).get("id") or requested
    )
    return upstream, mode, selected or {}
