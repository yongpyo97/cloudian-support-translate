import hashlib
import html
import re
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class MailText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
        self.link = None

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "style", "script"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")
        elif tag == "a" and not self.hidden:
            self.link = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in {"head", "style", "script"} and self.hidden:
            self.hidden -= 1
        elif tag == "a" and self.link:
            if self.link.startswith(("https://", "http://")):
                self.parts.append(f" {self.link} ")
            self.link = None

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def mail_content(raw, *, prefer_html=False):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    part = message.get_body(preferencelist=("html", "plain") if prefer_html else ("plain", "html"))
    body = part.get_content() if part else ""
    if part and part.get_content_type() == "text/html":
        parser = MailText()
        parser.feed(body)
        body = "".join(parser.parts)
    return str(message.get("Subject", "")), body.strip()


CLOUDIAN_PORTAL = re.compile(
    r"https://support\.cloudianhyperstore\.com/([A-Za-z0-9]{18}|[A-Za-z0-9]{15})(?![A-Za-z0-9])"
)
CLOUDIAN_ACK = re.compile(
    r"(?:\(NOTE\s*-\s*You may reply to this email directly but please DO NOT change the subject\. "
    r"This email content will be copied into a Case Comment and then emailed to other stakeholders "
    r"as well as you\.\) )?"
    r"Hello\s*, Thank you for opening a case with us\. "
    r"One of our Support Engineers will be responding to the case soon\. "
    r"Regards, Cloudian Technical Support",
    re.IGNORECASE,
)
CLOUDIAN_ACK_FOOTER = re.compile(
    r"(?: -{2,} (?P<signature>[^,]+, Cloudian Support) Working Shift: (?P<shift>.+?))?"
    r" You may reply to this email directly but please DO NOT change the subject\."
    r" Otherwise, click the below to confirm and update this case:",
    re.IGNORECASE,
)
CLOUDIAN_STAMP = re.compile(
    r"(?im)^[ \t]*Email from (Cloudian|Customer)[ \t]*-[ \t]*From:[^\r\n]*"
    r"(?:\r?\n[ \t]*\d{2}:\d{2}:\d{2}Z[ \t\\]*(?=\r?$))?"
)
CLOUDIAN_REPLY_FOOTER = re.compile(
    r"(?im)^\s*You may reply to this email directly but please DO NOT change the subject\."
)
CLOUDIAN_REPLY_NOTICE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"\(NOTE\s*-\s*You may reply to this email directly but please DO NOT change the subject\.\s+"
    r"This email content will be copied into a Case Comment and then emailed to other stakeholders\s+as well as you\.\)"
    r"|You may reply directly to this email\.\s+Keep the original email subject\.\s+"
    r"Your reply will be recorded in a Case Comment and emailed to stakeholders\."
    r"|You may reply to this email directly but please DO NOT change(?:[ \t]+the subject\.)?"
    r")[ \t]*$"
)
CLOUDIAN_COPY_WAIT = timedelta(seconds=30)
QUOTED_HEADER = re.compile(
    r"(?im)^[ \t>\\]*\*{0,2}(from|sent|date|to|cc|bcc|subject|"
    r"보낸 사람|보낸 날짜|받는 사람|참조|숨은 참조|제목)\*{0,2}[ \t]*[:：][^\r\n]*"
)
QUOTED_HEADER_LABELS = {
    "보낸 사람": "from", "보낸 날짜": "sent", "date": "sent", "받는 사람": "to",
    "참조": "cc", "숨은 참조": "bcc", "제목": "subject",
}


def compact_mail_text(text):
    text = re.sub(r"(?i)&(?:nbsp|#0*160|#x0*a0|#0*32|#x0*20);", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n(?:[ \t\u00a0]*\n){2,}", "\n\n", text).strip()


def remove_quoted_mail_headers(body):
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    fields = list(QUOTED_HEADER.finditer(body))
    blocks = []
    expected = ["from", "sent", "to", "subject"]
    for index, field in enumerate(fields):
        if field[1].lower() not in {"from", "보낸 사람"}:
            continue
        labels = []
        for header in fields[index:index + 6]:
            if body.count("\n", field.start(), header.end()) > 12:
                break
            name = header[1].lower()
            name = QUOTED_HEADER_LABELS.get(name, name)
            if name not in {"cc", "bcc"}:
                labels.append(name)
            if labels != expected[:len(labels)]:
                break
            if labels == expected:
                end = header.end()
                # Outlook can wrap a case title before the trailing reference token.
                wrapped = re.match(
                    r"\n(?:[^\S\n]*>)*[^\S\n]*[^\n]*\[\s*ref:![^\]\n]+:ref\s*\][ \t\\]*",
                    body[end:], re.I,
                )
                blocks.append((field.start(), end + (wrapped.end() if wrapped else 0)))
                break
    for start, end in reversed(blocks):
        body = body[:start] + body[end:]
    return compact_mail_text(body) if blocks else body


def cloudian_support_body(body):
    stamp = CLOUDIAN_STAMP.match(body)
    if stamp and stamp[1].lower() == "cloudian":
        remainder = body[stamp.end():]
        separator = re.match(r"\s*(?:\d{2}:\d{2}:\d{2}Z\s*)?\\?-{3,}[ \t]*\n", remainder)
        if separator:
            return remainder[separator.end():].lstrip()
    return body


def remove_cloudian_reply_notice(body):
    return compact_mail_text(CLOUDIAN_REPLY_NOTICE.sub("", body))


def clean_cloudian_metadata(body):
    if not CLOUDIAN_PORTAL.search(body) and not CLOUDIAN_STAMP.search(body):
        return body
    body = CLOUDIAN_STAMP.sub("", body)
    body = re.sub(r"(?im)^[ \t]*ref:![^\r\n]*?:ref[ \t\\]*$", "", body)
    body = re.sub(
        r"(?im)^[ \t]*(?:\[image\]\()?https://cloudian\.my\.salesforce\.com/"
        r"servlet/servlet\.ImageServer\?[^\r\n]*$",
        "",
        body,
    )
    body = re.sub(r"(?im)^[ \t]*--- Trimmed at \d+ bytes\.[^\r\n]*$", "", body)
    return compact_mail_text(body)


def clean_cloudian_case_confirmation(subject, body):
    if not re.match(r"(?i)^New Case\s", subject) or not CLOUDIAN_PORTAL.search(body):
        return body
    body = re.sub(
        r"(?is)[ \t]*If you have submitted an urgent P1 request and have an active\s+"
        r"support contract, you are advised to call the 24x7 Hotline Number\s+"
        r"shown in\s+the portal top page and below for a prompt response:",
        "",
        body,
        count=1,
    )
    priority = re.search(r"(?im)^\s*Priority:\s*((?:P\s*)?\d+)\b", body)
    if not priority or not re.fullmatch(r"(?i)(?:P\s*)?1", priority[1]):
        body = re.sub(
            r"(?is)24x7 Hotline for Severity 1 issues:\s*.*?(?=To view or update this case,)",
            "",
            body,
            count=1,
        )
    return compact_mail_text(body)


def cloudian_comment(body):
    """Return the visible new comment and its mailbox-independent fingerprint."""
    body = html.unescape(body)
    if not CLOUDIAN_PORTAL.search(body):
        return body, None, ""
    stamp = CLOUDIAN_STAMP.search(body)
    if stamp and stamp[1].lower() == "customer" and re.search(
        r"(?im)^\s*New Case Comment added:", body[:stamp.start()]
    ):
        outer_footer = list(re.finditer(
            r"(?im)^\s*Please click the below to confirm and update this case:", body
        ))
        if outer_footer:
            portal = CLOUDIAN_PORTAL.search(body, outer_footer[-1].end())
            if portal:
                comment = body[stamp.end():outer_footer[-1].start()]
                quote = re.search(
                    r"(?im)^\s*From:[^\r\n]+\r?\n(?:[ \t]*\r?\n)*[ \t]*Sent:",
                    comment,
                )
                context = ""
                if quote:
                    quoted = comment[quote.end():]
                    quoted = re.split(r"(?im)^\s*---\s*Trimmed at |^\s*From:.*\n\s*Sent:", quoted, maxsplit=1)[0]
                    context = re.sub(r"(?im)^\s*(?:To|Cc|Subject):[^\r\n]*", "", quoted).strip()
                    comment = comment[:quote.start()]
                comment = re.sub(r"(?m)^\s*-{3,}\s*$", "", comment).strip()
                if comment:
                    key = comment_fingerprint("customer", portal[1], comment)
                    return f"Customer case comment\n\n{comment}\n\nCase link\n{portal[0]}", key, context
    body = cloudian_support_body(body)
    footer = CLOUDIAN_REPLY_FOOTER.search(body)
    if footer and re.search(r"(?im)^\s*Working Shift:", body[:footer.start()]):
        portal = CLOUDIAN_PORTAL.search(body, footer.end())
        if portal:
            comment = re.sub(r"(?is)^\s*\(NOTE\s*-.*?\)\s*", "", body[:footer.start()]).strip()
            if comment:
                key = comment_fingerprint("support", portal[1], comment)
                return f"Cloudian Support case comment\n\n{comment}\n\nCase link\n{portal[0]}", key, ""
    return body, None, ""


def comment_fingerprint(author, case_id, comment):
    # The case portal can render the same comment as plain text or Markdown.
    comment = re.sub(r"(?m)^\s*[-*•]\s+", "", comment)
    comment = re.sub(r"\*\*|__", "", comment)
    comment = " ".join(comment.split())
    comment = re.sub(r"\s*:\s*", ": ", comment)
    digest = hashlib.sha256(comment.encode("utf-8")).hexdigest()
    return f"comment:{author}:{case_id[:15]}:{digest}"


def cloudian_support_comment_key(subject, body):
    case = re.match(r"(?i)^New Comment\s+\[(\d{1,12})\](?:\s|$)", subject)
    if not case:
        return None
    body = html.unescape(body)
    support = cloudian_support_body(body)
    footer = CLOUDIAN_REPLY_FOOTER.search(support)
    if support == body and not (footer and CLOUDIAN_PORTAL.search(support, footer.end())):
        return None
    shift = re.search(r"(?im)^[ \t]*Working Shift:", support[:footer.start()] if footer else support)
    if not shift:
        return None
    comment = re.sub(r"(?is)^\s*\(NOTE\s*-.*?\)\s*", "", support[:shift.start()]).strip()
    if comment:
        return comment_fingerprint("support", f"#{case[1]}", comment)
    return None


def cloudian_acknowledgement(body):
    body = re.sub(r"(?m)^([ \t]*)\\(?=-{3,}[ \t]*$)", r"\1", cloudian_support_body(body))
    normalized = " ".join(body.split())
    match = CLOUDIAN_ACK.match(normalized)
    if not match:
        return None
    portal = CLOUDIAN_PORTAL.search(normalized, match.end())
    if not portal:
        return None
    footer = CLOUDIAN_ACK_FOOTER.fullmatch(
        normalized[match.end():portal.start()].rstrip(" [<")
    )
    if not footer:
        return None
    tail = normalized[portal.end():].lstrip("]()> ")
    # Plain-text Markdown links and HTML anchors may repeat the visible URL.
    repeated = CLOUDIAN_PORTAL.match(tail)
    if repeated and repeated.group(1) == portal.group(1):
        tail = tail[repeated.end():].lstrip("])> ")
    if not re.fullmatch(
        r"(?:\[image\]\(https://cloudian\.my\.salesforce\.com/\S+\)\s*|"
        r"<https://cloudian\.my\.salesforce\.com/servlet/servlet\.ImageServer\?"
        r"[\w%&=.+~;/?: -]+>\s*)?"
        r"(?:ref:\S+:ref\s*)?"
        r"(?:Email from Cloudian - From: Cloudian - Case Reply , CC , @\d{4}-\d{2}-\d{2} "
        r"\d{2}:\d{2}:\d{2}Z)?",
        tail,
        flags=re.IGNORECASE,
    ):
        return None
    return portal.group(1)[:15], portal.group(), footer["signature"], footer["shift"]


def mail_source_language(body, subject=""):
    text = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+", "", body or subject)
    hangul = len(re.findall(r"[가-힣]", text))
    kana = len(re.findall(r"[ぁ-ゖァ-ヺ]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if hangul and hangul * 4 >= latin + kana:
        return "한국어"
    if kana and kana * 4 >= latin + hangul:
        return "일본어"
    return "영어"


def case_feedback_link(raw, number):
    _, content = mail_content(raw, prefer_html=True)
    links = set()
    for url in re.findall(r"https://(?:www\.)?getfeedback\.com/r/[A-Za-z0-9]+\?[^\s<>]+", content, re.I):
        parsed = urlsplit(html.unescape(url.rstrip(")]>")))
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if dict(query).get("Case_Case_Number", "").lstrip("0") != number.lstrip("0"):
            continue
        # The email sends one URL per rating. Leave the rating for the recipient to choose.
        neutral_query = [(key, value) for key, value in query if not key.startswith("gf_q[")]
        links.add(urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path,
                              urlencode(sorted(neutral_query)), "")))
    return next(iter(links)) if len(links) == 1 else None
