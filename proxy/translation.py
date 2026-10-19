"""Optional Gemini chapter route only. General chat/agents never use markers."""

import re
import secrets
from .providers import ProviderError


async def translate(source, send):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", source) if p.strip()]
    units = []
    for paragraph in paragraphs:
        while len(paragraph) > 2400:
            cut = paragraph.rfind(" ", 1200, 2400)
            if cut < 1200:
                cut = 2400
            units.append(paragraph[:cut])
            paragraph = paragraph[cut:].strip()
        if paragraph:
            units.append(paragraph)
    result = []
    start = 0
    while start < len(units):
        batch = []
        size = 0
        while start < len(units) and (not batch or size + len(units[start]) <= 2400):
            batch.append(units[start])
            size += len(units[start])
            start += 1
        nonce = secrets.token_hex(6)
        markers = [f"[[{nonce}_{i:06d}]]" for i in range(len(batch))]
        prompt = (
            "Translate every paragraph fully into Arabic. Preserve every marker exactly, without summaries. End with "
            + f"[[END_{nonce}]]\n"
            + "\n\n".join(marker + "\n" + text for marker, text in zip(markers, batch))
        )
        for attempt in range(3):
            answer = await send(prompt)
            matches = list(re.finditer(r"\[\[" + nonce + r"_(\d{6})\]\]", answer))
            end = f"[[END_{nonce}]]"
            if (
                len(matches) == len(batch)
                and [m[0] for m in matches] == markers
                and answer.strip().endswith(end)
                and not answer[: matches[0].start()].strip()
            ):
                translations = [
                    answer[
                        m.end() : (
                            matches[i + 1].start()
                            if i + 1 < len(matches)
                            else answer.rfind(end)
                        )
                    ].strip()
                    for i, m in enumerate(matches)
                ]
                if all(
                    t and (len(original) < 200 or len(t) > len(original) * 0.25)
                    for t, original in zip(translations, batch)
                ):
                    result.extend(translations)
                    break
            if attempt == 2:
                raise ProviderError(
                    "لم تكتمل تغطية فقرات Gemini؛ لم نحفظ فصلًا ناقصًا."
                )
    return "\n\n".join(result)
