import asyncio
from scrapling.fetchers import AsyncStealthySession

async def main():
    async with AsyncStealthySession(headless=True) as session:
        response = await session.fetch(
            "https://tesla.com/careers/search-jobs",
            network_idle=True,
            timeout=30000
        )
        print(f"Status: {response.status}")
        print(f"HTML length: {len(response.html_content)}")
        print(f"First 500: {response.html_content[:500]}")
        
        from selectolax.parser import HTMLParser
        tree = HTMLParser(response.html_content)
        title = tree.css_first("title")
        print(f"Title: {title.text() if title else 'none'}")
        body = tree.css_first("body")
        if body:
            text = body.text()
            print(f"Body text length: {len(text)}")
            print(f"Body text: {text[:2000]}")

asyncio.run(main())
