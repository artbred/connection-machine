"""
DOM Minifier for LLM Token Reduction.

Reduces HTML payload by 80-90% while preserving structure and semantic meaning
for selector generation. Uses Python's built-in HTMLParser - no external dependencies.
"""

import re
import logging
from html.parser import HTMLParser
from typing import Optional

logger = logging.getLogger(__name__)

KEEP_ATTRS = frozenset({
    'id', 'class', 'aria-label', 'aria-labelledby', 'aria-describedby',
    'role', 'type', 'data-control-name', 'href', 'name', 'placeholder', 'title',
})

REMOVE_ELEMENTS = frozenset({
    'script', 'style', 'svg', 'noscript', 'iframe', 'link', 'meta', 'head', 'template',
})

MAX_CLASSES = 4


class _PrefixComplete(Exception):
    """The parser produced more than the caller can use."""


class DOMMinifier(HTMLParser):
    def __init__(self, max_length: int | None = None):
        super().__init__()
        self.output = []
        self.remaining = max_length if max_length and max_length > 0 else None
        self.skip_depth = 0
        self.skip_element = None
        
    def _emit(self, text: str):
        # Each emitted fragment either is a tag or has already been stripped.
        compact = re.sub(r'\s+', ' ', text).replace('> <', '><')
        if self.remaining is not None:
            if len(compact) > self.remaining:
                self.output.append(compact[:self.remaining + 1])
                raise _PrefixComplete
            self.remaining -= len(compact)
        self.output.append(compact)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        if self.skip_depth > 0:
            if tag == self.skip_element:
                self.skip_depth += 1
            return
        
        if tag in REMOVE_ELEMENTS:
            self.skip_depth = 1
            self.skip_element = tag
            return
        
        filtered_attrs = []
        for name, value in attrs:
            if name not in KEEP_ATTRS or value is None:
                continue
            if name == 'class' and value:
                value = ' '.join(value.split()[:MAX_CLASSES])
            if value.strip():
                filtered_attrs.append((name, value))
        
        if filtered_attrs:
            attr_str = ' '.join(f'{n}="{v}"' for n, v in filtered_attrs)
            self._emit(f'<{tag} {attr_str}>')
        else:
            self._emit(f'<{tag}>')
    
    def handle_endtag(self, tag: str):
        if self.skip_depth > 0:
            if tag == self.skip_element:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_element = None
            return
        
        self._emit(f'</{tag}>')
    
    def handle_data(self, data: str):
        if self.skip_depth > 0:
            return
        
        text = ' '.join(data.split())
        if text:
            self._emit(text)
    
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        if self.skip_depth > 0 or tag in REMOVE_ELEMENTS:
            return
        
        filtered_attrs = []
        for name, value in attrs:
            if name not in KEEP_ATTRS or not value:
                continue
            if name == 'class' and value:
                value = ' '.join(value.split()[:MAX_CLASSES])
            if value.strip():
                filtered_attrs.append((name, value))
        
        if filtered_attrs:
            attr_str = ' '.join(f'{n}="{v}"' for n, v in filtered_attrs)
            self._emit(f'<{tag} {attr_str}/>')
        else:
            self._emit(f'<{tag}/>')
    
    def get_minified(self) -> str:
        return ''.join(self.output)


def minify_dom(html: str, max_length: Optional[int] = None) -> str:
    try:
        parser = DOMMinifier(max_length)
        try:
            parser.feed(html)
        except _PrefixComplete:
            pass
        result = parser.get_minified()
        
        if max_length and len(result) > max_length:
            truncated = result[:max_length]
            last_tag_end = truncated.rfind('>')
            if last_tag_end > max_length * 0.8:
                truncated = truncated[:last_tag_end + 1]
            result = truncated + '...[truncated]'
        
        return result
        
    except Exception as e:
        logger.error(f"DOM minification failed: {e}")
        result = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        result = re.sub(r'<style[^>]*>.*?</style>', '', result, flags=re.DOTALL | re.IGNORECASE)
        result = re.sub(r'\s+', ' ', result)
        return result[:max_length] if max_length else result


def extract_profile_section(html: str) -> str:
    patterns = [
        r'<section[^>]*class="[^"]*pv-top-card[^"]*"[^>]*>.*?</section>',
        r'<div[^>]*class="[^"]*pvs-profile-actions[^"]*"[^>]*>.*?</div>',
        r'<div[^>]*class="[^"]*artdeco-dropdown__content[^"]*"[^>]*>.*?</div>',
    ]
    
    sections = []
    for pattern in patterns:
        matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
        sections.extend(matches)
    
    if sections:
        return '\n'.join(sections)
    
    return html[:50000]
