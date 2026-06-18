import re

def clean_minutes_html(raw_minutes_html: str) -> str:
    """Sanitizes AI-generated HTML by removing rogue tags and excessive breaks."""
    if not raw_minutes_html:
        return "<p>No minutes recorded.</p>"

    clean_minutes = re.sub(r'</?div[^>]*>', '', raw_minutes_html)
    clean_minutes = re.sub(r'(<br\s*/?>\s*){3,}', '<br><br>', clean_minutes)
    return clean_minutes

def generate_styled_document(metadata: dict, clean_minutes: str) -> str:
    """Wraps cleaned HTML inside a styled template for presentation."""
    return f"""
    <!DOCTYPE html> 
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>{metadata['title']}</title>
        <style>
            body {{ font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 850px; margin: 40px auto; padding: 40px; background-color: #ffffff; box-shadow: 0 4px 15px rgba(0,0,0,0.08); }}
            .header-container {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #eaeaea; padding-bottom: 15px; margin-bottom: 20px; font-size: 15px; }}
            .header-item {{ display: flex; flex-direction: column; }}
            .header-label {{ color: #333; font-weight: normal; }}
            .header-value {{ color: #000; }}
            .meta-section {{ margin-bottom: 25px; font-size: 16px; }}
            .meta-section p {{ margin: 5px 0; }}
            .minutes-body {{ text-align: left; margin-bottom: 40px; min-height: 300px; white-space: pre-wrap; font-family: inherit; }}
        </style>
    </head>
    <body>
        <div class="header-container">
            <div class="header-item"><span class="header-label">State:</span><span class="header-value">Odisha</span></div>
            <div class="header-item"><span class="header-label">ZP: {metadata['zp_name']}</span><span class="header-value">({metadata['zp_code']})</span></div>
            <div class="header-item"><span class="header-label">BP: {metadata['bp_name']}</span><span class="header-value">({metadata['bp_code']})</span></div>
            <div class="header-item"><span class="header-label">GP: {metadata['gp_name']}</span><span class="header-value">({metadata['gp_code']})</span></div>
        </div>
        <div class="meta-section">
            <p><strong>Meeting Type:</strong> {metadata['meeting_type']}</p>
            <p><strong>Meeting Title:</strong> {metadata['title']}</p>
            <p><strong>Meeting Date:</strong> {metadata['date']}</p>
        </div>
        <div class="minutes-body">{clean_minutes}</div>
    </body>
    </html>
    """