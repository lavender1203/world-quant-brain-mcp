# WorldQuant Brain MCP Server

A Model Context Protocol (MCP) server that provides integration with the WorldQuant Brain API, enabling AI assistants to interact with quantitative research tools for developing and testing trading alphas.

## Features

- **Submit Alpha**: Submit alpha expressions to WorldQuant Brain for simulation
- **Get Alpha Details**: Retrieve information about specific alphas
- **List Alphas**: View all alphas in your account with their status and metrics
- **Get Simulation Results**: Access detailed simulation results including Sharpe ratio, returns, turnover, fitness, and drawdown

## Prerequisites

- Python 3.10 or higher
- A WorldQuant Brain account (sign up at https://platform.worldquantbrain.com/)
- WorldQuant Brain API credentials (email and password)

## Installation

### Using pip

```bash
pip install -e .
```

### Using uv (recommended)

```bash
uv pip install -e .
```

## Configuration

Set your WorldQuant Brain credentials as environment variables:

```bash
export WORLDQUANT_EMAIL="your-email@example.com"
export WORLDQUANT_PASSWORD="your-password"
```

Or create a `.env` file in the project root:

```
WORLDQUANT_EMAIL=your-email@example.com
WORLDQUANT_PASSWORD=your-password
```

## Usage

### Running the MCP Server

```bash
python -m worldquant_brain_mcp.server
```

Or use the installed script:

```bash
worldquant-brain-mcp
```

### Integration with Claude Desktop

Add this to your Claude Desktop configuration file (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "worldquant-brain": {
      "command": "python",
      "args": ["-m", "worldquant_brain_mcp.server"],
      "env": {
        "WORLDQUANT_EMAIL": "your-email@example.com",
        "WORLDQUANT_PASSWORD": "your-password"
      }
    }
  }
}
```

### Available Tools

#### submit_alpha

Submit an alpha expression to WorldQuant Brain for simulation.

**Parameters:**
- `alpha_expression` (required): The alpha expression to submit (e.g., `"rank(close)"`)
- `region` (optional): Region for simulation (default: "USA")
- `universe` (optional): Universe for simulation (default: "TOP3000")
- `delay` (optional): Trading delay in days, 0 or 1 (default: 1)

**Example:**
```json
{
  "alpha_expression": "rank(close)",
  "region": "USA",
  "universe": "TOP3000",
  "delay": 1
}
```

#### get_alpha

Get details about a specific alpha by its ID.

**Parameters:**
- `alpha_id` (required): The ID of the alpha to retrieve

#### list_alphas

List all alphas in your account.

**Parameters:**
- `limit` (optional): Maximum number of alphas to return (default: 10)

#### get_simulation

Get simulation results for a specific alpha.

**Parameters:**
- `alpha_id` (required): The ID of the alpha

## Alpha Expression Examples

Here are some example alpha expressions you can try:

- **Simple rank**: `rank(close)`
- **Momentum**: `close / ts_delay(close, 5) - 1`
- **Mean reversion**: `(close - ts_mean(close, 20)) / ts_std_dev(close, 20)`
- **Volume-weighted**: `rank(volume) * rank(returns)`
- **Technical**: `ts_delta(close, 1) / close`

## Development

### Installing Development Dependencies

```bash
pip install -e ".[dev]"
```

### Code Formatting

```bash
black worldquant_brain_mcp/
```

### Linting

```bash
ruff check worldquant_brain_mcp/
```

## About WorldQuant Brain

WorldQuant Brain is a web-based platform for developing, testing, and refining quantitative trading strategies (alphas). The platform provides:

- Historical market data for backtesting
- Simulation engine for evaluating alpha performance
- Performance metrics including Sharpe ratio, returns, turnover, and fitness
- Support for multiple regions and universes
- Alpha expression language for defining trading signals

Learn more at: https://platform.worldquantbrain.com/

## About MCP

The Model Context Protocol (MCP) is an open protocol that enables seamless integration between LLM applications and external data sources. This server implements MCP to allow AI assistants like Claude to interact with the WorldQuant Brain API.

Learn more at: https://modelcontextprotocol.io/

## License

MIT License

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Troubleshooting

### Authentication Issues

If you encounter authentication errors:
1. Verify your credentials are correct
2. Check that your WorldQuant Brain account is active
3. Ensure environment variables are properly set

### API Rate Limits

WorldQuant Brain may have rate limits on API calls. If you encounter rate limiting:
- Reduce the frequency of requests
- Use the `list_alphas` tool with a smaller limit
- Wait before retrying failed requests

### Connection Issues

If the server fails to connect:
1. Check your internet connection
2. Verify the WorldQuant Brain service is available
3. Check for any firewall or proxy settings that might block the connection

## Support

For issues related to:
- **This MCP server**: Open an issue on GitHub
- **WorldQuant Brain platform**: Contact WorldQuant support
- **MCP protocol**: Visit the MCP documentation

## Disclaimer

This is an unofficial integration with WorldQuant Brain. Use at your own risk. Always verify simulation results and thoroughly test any trading strategies before deployment.