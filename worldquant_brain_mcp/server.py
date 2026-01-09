"""MCP Server for WorldQuant Brain API."""

import os
import json
import logging
from typing import Any, Optional

from mcp.server import Server
from mcp.types import Tool, TextContent
import mcp.server.stdio

from worldquant_brain_mcp.client import BrainClient, BrainError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worldquant-brain-mcp")

# Create server instance
app = Server("worldquant-brain-mcp")

# Global API client
_api_client: Optional[BrainClient] = None


def get_api_client() -> BrainClient:
    """Get or create WorldQuant Brain API client."""
    global _api_client
    if _api_client is None:
        email = os.getenv("WORLDQUANT_EMAIL")
        password = os.getenv("WORLDQUANT_PASSWORD")
        
        if not email or not password:
            raise ValueError(
                "WORLDQUANT_EMAIL and WORLDQUANT_PASSWORD environment variables must be set"
            )
        
        _api_client = BrainClient(email=email, password=password)
        logger.info("WorldQuant Brain API client initialized")
    
    return _api_client


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="submit_alpha",
            description="Submit an alpha expression to WorldQuant Brain for simulation. "
                       "Returns simulation results including performance metrics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "alpha_expression": {
                        "type": "string",
                        "description": "The alpha expression to submit (e.g., 'rank(close)')",
                    },
                    "region": {
                        "type": "string",
                        "description": "Region for simulation (e.g., 'USA', 'CHN', 'EUR')",
                        "default": "USA",
                    },
                    "universe": {
                        "type": "string",
                        "description": "Universe for simulation (e.g., 'TOP3000', 'TOP500')",
                        "default": "TOP3000",
                    },
                    "delay": {
                        "type": "integer",
                        "description": "Trading delay in days (0 or 1)",
                        "default": 1,
                    },
                },
                "required": ["alpha_expression"],
            },
        ),
        Tool(
            name="get_alpha",
            description="Get details about a specific alpha by its ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "alpha_id": {
                        "type": "string",
                        "description": "The ID of the alpha to retrieve",
                    },
                },
                "required": ["alpha_id"],
            },
        ),
        Tool(
            name="list_alphas",
            description="List all alphas in your account with their status and performance metrics",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of alphas to return",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="get_simulation",
            description="Get simulation results for a specific alpha including performance metrics "
                       "like Sharpe ratio, returns, turnover, fitness, and drawdown",
            inputSchema={
                "type": "object",
                "properties": {
                    "alpha_id": {
                        "type": "string",
                        "description": "The ID of the alpha",
                    },
                },
                "required": ["alpha_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Handle tool calls."""
    try:
        client = get_api_client()
        
        if name == "submit_alpha":
            alpha_expression = arguments["alpha_expression"]
            region = arguments.get("region", "USA")
            universe = arguments.get("universe", "TOP3000")
            delay = arguments.get("delay", 1)
            
            logger.info(f"Submitting alpha: {alpha_expression}")
            
            # Submit simulation
            simulation_id = client.submit_simulation(
                expression=alpha_expression,
                region=region,
                universe=universe,
                delay=delay,
            )
            
            logger.info(f"Simulation submitted: {simulation_id}")
            
            # Wait for simulation to complete
            simulation_result = client.wait_for_simulation(simulation_id)
            
            # Get alpha details
            alpha_id = simulation_result.get("alpha")
            alpha_details = client.get_alpha(alpha_id) if alpha_id else {}
            
            result = {
                "simulation_id": simulation_id,
                "alpha_id": alpha_id,
                "status": simulation_result.get("status"),
                "expression": alpha_expression,
                "region": region,
                "universe": universe,
                "delay": delay,
                "alpha_details": alpha_details,
            }
            
            return [
                TextContent(
                    type="text",
                    text=f"Alpha submitted successfully!\n\n{json.dumps(result, indent=2)}",
                )
            ]
        
        elif name == "get_alpha":
            alpha_id = arguments["alpha_id"]
            logger.info(f"Getting alpha: {alpha_id}")
            
            alpha = client.get_alpha(alpha_id)
            
            return [
                TextContent(
                    type="text",
                    text=json.dumps(alpha, indent=2),
                )
            ]
        
        elif name == "list_alphas":
            limit = arguments.get("limit", 10)
            logger.info(f"Listing alphas (limit: {limit})")
            
            alphas = client.list_alphas(limit=limit)
            
            result = {
                "count": len(alphas),
                "alphas": alphas,
            }
            
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]
        
        elif name == "get_simulation":
            alpha_id = arguments["alpha_id"]
            logger.info(f"Getting simulation details for alpha: {alpha_id}")
            
            # Get alpha which contains simulation results
            alpha = client.get_alpha(alpha_id)
            
            # Extract simulation-specific metrics from the alpha details
            # The API returns alpha details which include simulation metrics
            result = {
                "alpha_id": alpha_id,
                "sharpe": alpha.get("sharpe"),
                "returns": alpha.get("returns"),
                "turnover": alpha.get("turnover"),
                "fitness": alpha.get("fitness"),
                "drawdown": alpha.get("drawdown"),
                "margin": alpha.get("margin"),
                "longCount": alpha.get("longCount"),
                "shortCount": alpha.get("shortCount"),
                "is_best": alpha.get("is_best"),
                "expression": alpha.get("regular"),
                "status": alpha.get("status"),
            }
            
            return [
                TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )
            ]
        
        else:
            return [
                TextContent(
                    type="text",
                    text=f"Unknown tool: {name}",
                )
            ]
    
    except BrainError as e:
        logger.error(f"Brain API error in {name}: {str(e)}")
        return [
            TextContent(
                type="text",
                text=f"Brain API Error: {str(e)}",
            )
        ]
    except Exception as e:
        logger.error(f"Error in {name}: {str(e)}")
        return [
            TextContent(
                type="text",
                text=f"Error: {str(e)}",
            )
        ]


async def main():
    """Run the MCP server."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
