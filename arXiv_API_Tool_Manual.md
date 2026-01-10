# 🔍 arXiv Paper Search & Download Tool

A comprehensive Python tool for searching, analyzing, and downloading research papers from arXiv using their public API. Perfect for researchers, students, and anyone interested in academic papers.

## 📋 Table of Contents

- [Features](#-features)
- [Installation](#-installation)
- [Quick Start](#-quick-start)
- [Usage Modes](#-usage-modes)
- [API Functions](#-api-functions)
- [Examples](#-examples)
- [Advanced Usage](#-advanced-usage)
- [Troubleshooting](#-troubleshooting)

## ✨ Features

- **🔍 Smart Search**: Search arXiv papers by title, author, abstract, or any keyword
- **📥 Smart Download**: Download PDFs with automatic filename renaming to paper titles
- **📊 Result Parsing**: Automatically extract structured information (title, authors, abstract, ID)
- **🖥️ Interactive Mode**: Command-line interface for easy searching and downloading
- **⚡ Batch Operations**: Search multiple papers and download in sequence
- **📈 Academic Research**: Perfect for literature reviews and research discovery
- **🔄 Auto-Rename**: Downloaded files are automatically named using paper titles instead of cryptic IDs

## 🚀 Installation

### Prerequisites
- Python 3.6 or higher
- Internet connection for API access

### Install Dependencies
```bash
pip install requests
```

### Download the Script
```bash
# Clone or download arxiv_api.py to your working directory
```

## 🎯 Quick Start

### Basic Search
```bash
python arxiv_api.py "machine learning"
```

### Search with Custom Results
```bash
python arxiv_api.py "quantum computing" -n 10
```

### Search and Download First Result
```bash
python arxiv_api.py "deep learning" -d
```

### Interactive Mode
```bash
python arxiv_api.py -i
```

### Download Paper by ID (with auto-rename)
```bash
# In interactive mode:
# 📚 arxiv> download 2502.05218v1
# This will automatically rename the file to the paper's title
```

## 🎮 Usage Modes

### 1. Command Line Mode
Direct search queries from the command line.

**Syntax:**
```bash
python arxiv_api.py [query] [options]
```

**Options:**
- `-n, --max_results`: Maximum number of results (default: 5)
- `-d, --download`: Download the first result automatically
- `-i, --interactive`: Start interactive mode
- `-h, --help`: Show help message

### 2. Interactive Mode
Interactive command-line interface for multiple operations.

**Commands:**
- `search <query> [max_results]`: Search for papers
- `download <paper_id>`: Download a specific paper (with auto-rename)
- `help`: Show available commands
- `quit/exit`: Exit the program

## 🔧 API Functions

### Core Functions

#### `search_arxiv(query, max_results=10)`
Searches arXiv for papers using the public API.

**Parameters:**
- `query` (str): Search query string
- `max_results` (int): Maximum number of results (default: 10)

**Returns:**
- `str`: XML response from arXiv API

**Example:**
```python
from arxiv_api import search_arxiv

results = search_arxiv("artificial intelligence", max_results=5)
```

#### `get_paper_metadata(paper_id)`
Fetches paper metadata directly from arXiv API using paper ID.

**Parameters:**
- `paper_id` (str): arXiv paper ID (e.g., "2502.05218v1")

**Returns:**
- `dict`: Paper information dictionary, or `None` if not found

**Example:**
```python
from arxiv_api import get_paper_metadata

paper_info = get_paper_metadata("2502.05218v1")
if paper_info:
    print(f"Title: {paper_info['title']}")
    print(f"Authors: {', '.join(paper_info['authors'])}")
```

#### `download_paper(paper_id, output_dir=".", paper_title=None)`
Downloads a specific paper by its arXiv ID and automatically renames it to the paper title.

**Parameters:**
- `paper_id` (str): arXiv paper ID (e.g., "2502.05218v1")
- `output_dir` (str): Output directory (default: current directory)
- `paper_title` (str): Paper title for filename (optional, will be fetched automatically if not provided)

**Returns:**
- `str`: File path of downloaded PDF, or `None` if failed

**Features:**
- **Auto-rename**: Automatically renames downloaded files to paper titles
- **Smart cleaning**: Removes special characters and limits filename length
- **Fallback**: Uses paper ID if title is unavailable

**Example:**
```python
from arxiv_api import download_paper

# Download with automatic title fetching and renaming
filepath = download_paper("2502.05218v1")

# Download with custom title
filepath = download_paper("2502.05218v1", paper_title="My Custom Title")
```

#### `parse_search_results(xml_content)`
Parses XML search results and extracts structured paper information.

**Parameters:**
- `xml_content` (str): XML response from arXiv API

**Returns:**
- `list`: List of dictionaries containing paper information

**Paper Information Structure:**
```python
{
    'title': 'Paper Title',
    'authors': ['Author 1', 'Author 2'],
    'abstract': 'Paper abstract...',
    'paper_id': '2502.05218v1',
    'published': '2025-02-05T12:37:15Z'
}
```

#### `search_and_download(query, max_results=5, download_first=False)`
Combined function that searches for papers and optionally downloads the first result.

**Parameters:**
- `query` (str): Search query string
- `max_results` (int): Maximum number of results (default: 5)
- `download_first` (bool): Whether to download first result (default: False)

**Example:**
```python
from arxiv_api import search_and_download

# Search and display results only
search_and_download("machine learning", max_results=3)

# Search and download first result (with auto-rename)
search_and_download("deep learning", max_results=5, download_first=True)
```

### Interactive Mode Functions

#### `interactive_mode()`
Starts the interactive command-line interface.

**Features:**
- Command history
- Error handling
- User-friendly prompts
- Multiple search sessions
- **Smart download with auto-rename**

## 📚 Examples

### Example 1: Basic Paper Search
```bash
# Search for machine learning papers
python arxiv_api.py "machine learning"

# Output:
# Searching arXiv for: 'machine learning'
# --------------------------------------------------
# Found 5 papers:
# 
# 1. Title: Introduction to Machine Learning
#    Authors: John Doe, Jane Smith
#    Paper ID: 2103.12345
#    Published: 2021-03-15T10:30:00Z
#    Abstract: This paper introduces...
```

### Example 2: Search with Custom Results
```bash
# Get 10 results for quantum computing
python arxiv_api.py "quantum computing" -n 10
```

### Example 3: Search and Download (with auto-rename)
```bash
# Search for papers and download the first one
python arxiv_api.py "artificial intelligence" -d
# Downloaded file will be automatically renamed to the paper title
```

### Example 4: Interactive Mode with Smart Download
```bash
python arxiv_api.py -i

# 📚 arxiv> search blockchain finance 5
# 📚 arxiv> download 2502.05218v1
# Fetching paper information for 2502.05218v1...
# Found paper: FactorGCL: A Hypergraph-Based Factor Model...
# Downloaded: .\FactorGCL_A_Hypergraph-Based_Factor_Model...pdf
# 📚 arxiv> help
# 📚 arxiv> quit
```

### Example 5: Python Script Integration
```python
from arxiv_api import search_and_download, download_paper, get_paper_metadata

# Search for papers on a specific topic
search_and_download("quantitative finance China", max_results=3)

# Download a specific paper with auto-rename
download_paper("2502.05218v1")

# Get paper metadata
paper_info = get_paper_metadata("2502.05218v1")
if paper_info:
    print(f"Title: {paper_info['title']}")
```

## 🔍 Advanced Usage

### Smart Download Features

#### Automatic Filename Generation
```python
from arxiv_api import download_paper

# The tool automatically:
# 1. Fetches paper metadata
# 2. Extracts the title
# 3. Cleans the title for filename use
# 4. Downloads and renames the file

# Example output filename:
# "FactorGCL_A_Hypergraph-Based_Factor_Model_with_Temporal_Residual_Contrastive_Learning_for_Stock_Returns_Prediction.pdf"
```

#### Custom Search Queries

##### Field-Specific Searches
```bash
# Search by author
python arxiv_api.py "au:Yann LeCun"

# Search by title
python arxiv_api.py "ti:deep learning"

# Search by abstract
python arxiv_api.py "abs:neural networks"

# Search by category
python arxiv_api.py "cat:cs.AI"
```

##### Complex Queries
```bash
# Multiple terms
python arxiv_api.py "machine learning AND neural networks"

# Exclude terms
python arxiv_api.py "deep learning NOT reinforcement"

# Date range
python arxiv_api.py "machine learning AND submittedDate:[20230101 TO 20231231]"
```

### Batch Operations

#### Download Multiple Papers with Auto-Rename
```python
from arxiv_api import search_arxiv, parse_search_results, download_paper

# Search for papers
query = "quantum computing"
results = search_arxiv(query, max_results=10)
papers = parse_search_results(results)

# Download all papers (each will be automatically renamed)
for paper in papers:
    paper_id = paper.get('paper_id')
    if paper_id:
        download_paper(paper_id, output_dir="./quantum_papers")
```

#### Custom Output Formatting
```python
from arxiv_api import search_and_download

# Custom display function
def custom_display(papers):
    for i, paper in enumerate(papers, 1):
        print(f"📄 Paper {i}: {paper['title']}")
        print(f"👥 Authors: {', '.join(paper['authors'])}")
        print(f"🆔 ID: {paper['paper_id']}")
        print(f"📅 Date: {paper['published']}")
        print(f"📝 Abstract: {paper['abstract'][:150]}...")
        print("-" * 80)

# Use custom display
search_and_download("blockchain", max_results=3)
```

## 🛠️ Troubleshooting

### Common Issues

#### 1. No Results Found
**Problem:** Search returns no papers
**Solution:** 
- Check spelling and use broader terms
- Try different keyword combinations
- Verify internet connection

#### 2. Download Failed
**Problem:** Paper download fails
**Solution:**
- Verify paper ID is correct
- Check if paper exists on arXiv
- Ensure write permissions in output directory

#### 3. API Rate Limiting
**Problem:** Too many requests
**Solution:**
- Wait between requests
- Reduce batch size
- Use interactive mode for multiple searches

#### 4. XML Parsing Errors
**Problem:** Error parsing search results
**Solution:**
- Check internet connection
- Verify API response format
- Update the script if needed

#### 5. Filename Too Long
**Problem:** Generated filename exceeds system limits
**Solution:**
- The tool automatically limits filenames to 100 characters
- Special characters are automatically cleaned
- Fallback to paper ID if title is unavailable

### Error Messages

```
Error: Failed to download paper 2502.05218v1
```
- Paper ID may not exist
- Network connection issue
- arXiv server problem

```
Error parsing XML: ...
```
- Malformed API response
- Network interruption
- API format change

```
Could not find paper information for 2502.05218v1
```
- Paper ID may be invalid
- arXiv API issue
- Network connectivity problem

## 📖 API Reference

### arXiv API Endpoints
- **Search API**: `http://export.arxiv.org/api/query`
- **Metadata API**: `http://export.arxiv.org/api/query?id_list={paper_id}`
- **Documentation**: https://arxiv.org/help/api
- **Rate Limits**: Be respectful, avoid excessive requests

### Data Fields Available
- **Title**: Paper title
- **Authors**: List of author names
- **Abstract**: Paper abstract
- **Paper ID**: Unique arXiv identifier
- **Published Date**: Publication timestamp
- **Categories**: arXiv subject categories

### Paper ID Format
- **Format**: `YYMM.NNNNNvN`
- **Example**: `2502.05218v1`
- **Download URL**: `https://arxiv.org/pdf/{paper_id}.pdf`

### Smart Download Features
- **Automatic Metadata Fetching**: Gets paper information before download
- **Intelligent Filename Generation**: Converts paper titles to valid filenames
- **Character Cleaning**: Removes special characters and spaces
- **Length Limiting**: Ensures filenames don't exceed system limits
- **Fallback Naming**: Uses paper ID if title is unavailable

## 🤝 Contributing

### Adding New Features
1. Fork the repository
2. Create a feature branch
3. Implement your changes
4. Add tests and documentation
5. Submit a pull request

### Reporting Issues
- Check existing issues first
- Provide detailed error messages
- Include system information
- Describe steps to reproduce

## 📄 License

This project is open source and available under the MIT License.

## 🙏 Acknowledgments

- **arXiv**: For providing the public API
- **Python Community**: For excellent libraries and tools
- **Researchers**: For contributing to open science

## 📞 Support

### Getting Help
- Check this documentation first
- Review the examples section
- Search existing issues
- Create a new issue for bugs

### Useful Links
- [arXiv Official Site](https://arxiv.org/)
- [arXiv API Documentation](https://arxiv.org/help/api)
- [Python Requests Library](https://requests.readthedocs.io/)

---

**Happy Researching! 🎓📚**

*This tool makes academic research more accessible and efficient. Use it responsibly and respect arXiv's terms of service.*
