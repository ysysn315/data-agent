from langchain.tools import tool
from loguru import logger
import httpx
import json

def create_tavily_search_tool(api_key: str, base_url: str = "https://api.tavily.com"):
    @tool
    async def tavily_search(query:str,max_results:int=3)->str:
        """联网搜索最新网页信息。适用于知识库中没有答案、需要最新信息的场景。"""
        if not api_key:
            return json.dumps(
                {"success": False, "error": "TAVILY_API_KEY not configured"},
                ensure_ascii=False
            )
        try:
            normalized_max_results = int(max_results)
        except (TypeError, ValueError):
            normalized_max_results = 3
        normalized_max_results = max(1, min(normalized_max_results, 8))

        payload={
            "api_key":api_key,
            "query":query,
            "max_results":normalized_max_results,
            "search_depth":"basic",
            "include_answer":True
        }
        url = f"{base_url.rstrip('/')}/search"

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp=await client.post(url,json=payload)
                resp.raise_for_status()
                data=resp.json()
            results=[]
            for item in data.get("results",[])[:8]:
                results.append({
                    "title":item.get("title",""),
                    "url":item.get("url",""),
                    "content":(item.get("content","") or "")[:500]
                })    
            return json.dumps(
                {
                    "success":True,
                    "query":query,
                    "answer":data.get("answer",""),
                    "results":results
                },
                ensure_ascii=False
            )
        except Exception as e:
            logger.error(f"tavily_search failed: {e}")
            return json.dumps(
                {"success": False, "error": str(e), "query": query},
                ensure_ascii=False
            )
    return tavily_search

        
