import requests
import xml.etree.ElementTree as ET
import os
import sys
import argparse

def search_arxiv(query, max_results=10):
    """Search arXiv for papers"""
    base_url = "http://export.arxiv.org/api/query"
    params = {
        'search_query': query,
        'start': 0,
        'max_results': max_results
    }
    
    response = requests.get(base_url, params=params)
    return response.text

def get_paper_metadata(paper_id):
    """Get paper metadata directly from arXiv API"""
    try:
        # Use the arXiv API to get paper metadata
        metadata_url = f"http://export.arxiv.org/api/query?id_list={paper_id}"
        response = requests.get(metadata_url)
        
        if response.status_code == 200:
            papers = parse_search_results(response.text)
            if papers and len(papers) > 0:
                return papers[0]
        return None
    except Exception as e:
        print(f"Error fetching paper metadata: {e}")
        return None

def download_paper(paper_id, output_dir=".", paper_title=None):
    """Download a paper by its ID and rename it to the paper title"""
    pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"
    response = requests.get(pdf_url)
    
    if response.status_code == 200:
        # Create filename from paper title if available, otherwise use paper ID
        if paper_title:
            # Clean the title for filename (remove special characters, limit length)
            clean_title = "".join(c for c in paper_title if c.isalnum() or c in (' ', '-', '_')).rstrip()
            clean_title = clean_title.replace(' ', '_')[:100]  # Limit length to 100 chars
            filename = f"{clean_title}.pdf"
        else:
            filename = f"{paper_id}.pdf"
        
        filepath = os.path.join(output_dir, filename)
        
        with open(filepath, 'wb') as f:
            f.write(response.content)
        print(f"Downloaded: {filepath}")
        return filepath
    else:
        print(f"Failed to download paper {paper_id}")
        return None

def parse_search_results(xml_content):
    """Parse XML search results and extract paper information"""
    try:
        root = ET.fromstring(xml_content)
        papers = []
        
        # Find all entry elements
        for entry in root.findall('.//{http://www.w3.org/2005/Atom}entry'):
            paper_info = {}
            
            # Extract title
            title_elem = entry.find('.//{http://www.w3.org/2005/Atom}title')
            if title_elem is not None:
                paper_info['title'] = title_elem.text.strip()
            
            # Extract authors
            authors = []
            for author in entry.findall('.//{http://www.w3.org/2005/Atom}author'):
                name_elem = author.find('.//{http://www.w3.org/2005/Atom}name')
                if name_elem is not None:
                    authors.append(name_elem.text.strip())
            paper_info['authors'] = authors
            
            # Extract abstract
            summary_elem = entry.find('.//{http://www.w3.org/2005/Atom}summary')
            if summary_elem is not None:
                paper_info['abstract'] = summary_elem.text.strip()
            
            # Extract paper ID from the id field
            id_elem = entry.find('.//{http://www.w3.org/2005/Atom}id')
            if id_elem is not None:
                # Extract ID from URL like "http://arxiv.org/abs/2103.12345"
                paper_id = id_elem.text.split('/')[-1]
                paper_info['paper_id'] = paper_id
            
            # Extract published date
            published_elem = entry.find('.//{http://www.w3.org/2005/Atom}published')
            if published_elem is not None:
                paper_info['published'] = published_elem.text.strip()
            
            papers.append(paper_info)
        
        return papers
    except ET.ParseError as e:
        print(f"Error parsing XML: {e}")
        return []

def search_and_download(query, max_results=5, download_first=False):
    """Search for papers and optionally download the first result"""
    print(f"Searching arXiv for: '{query}'")
    print("-" * 50)
    
    # Search for papers
    results = search_arxiv(query, max_results)
    papers = parse_search_results(results)
    
    if not papers:
        print("No papers found.")
        return
    
    # Display search results
    print(f"Found {len(papers)} papers:\n")
    for i, paper in enumerate(papers, 1):
        print(f"{i}. Title: {paper.get('title', 'N/A')}")
        print(f"   Authors: {', '.join(paper.get('authors', ['N/A']))}")
        print(f"   Paper ID: {paper.get('paper_id', 'N/A')}")
        print(f"   Published: {paper.get('published', 'N/A')}")
        print(f"   Abstract: {paper.get('abstract', 'N/A')[:200]}...")
        print()
    
    # Optionally download first paper
    if download_first and papers:
        first_paper = papers[0]
        paper_id = first_paper.get('paper_id')
        paper_title = first_paper.get('title')
        if paper_id:
            print(f"Downloading first paper: {paper_id}")
            download_paper(paper_id, paper_title=paper_title)
        else:
            print("Could not extract paper ID for download")

def interactive_mode():
    """Interactive mode for searching arXiv"""
    print("🔍 arXiv Paper Search Tool")
    print("=" * 40)
    print("Commands:")
    print("  search <query> [max_results] - Search for papers")
    print("  download <paper_id> - Download a specific paper")
    print("  help - Show this help message")
    print("  quit/exit - Exit the program")
    print()
    
    while True:
        try:
            command = input("📚 arxiv> ").strip()
            
            if not command:
                continue
                
            parts = command.split()
            cmd = parts[0].lower()
            
            if cmd in ['quit', 'exit', 'q']:
                print("Goodbye! 👋")
                break
                
            elif cmd == 'help':
                print("Commands:")
                print("  search <query> [max_results] - Search for papers")
                print("  download <paper_id> - Download a specific paper")
                print("  help - Show this help message")
                print("  quit/exit - Exit the program")
                print()
                
            elif cmd == 'search':
                if len(parts) < 2:
                    print("Usage: search <query> [max_results]")
                    continue
                    
                query = ' '.join(parts[1:-1]) if len(parts) > 2 else parts[1]
                max_results = int(parts[-1]) if len(parts) > 2 and parts[-1].isdigit() else 5
                
                search_and_download(query, max_results, download_first=False)
                
            elif cmd == 'download':
                if len(parts) < 2:
                    print("Usage: download <paper_id>")
                    continue
                    
                paper_id = parts[1]
                # Get paper metadata first
                print(f"Fetching paper information for {paper_id}...")
                paper_info = get_paper_metadata(paper_id)
                
                if paper_info and paper_info.get('title'):
                    paper_title = paper_info['title']
                    print(f"Found paper: {paper_title}")
                    download_paper(paper_id, paper_title=paper_title)
                else:
                    print(f"Could not find paper information for {paper_id}")
                    print("Downloading with paper ID as filename...")
                    download_paper(paper_id)
                
            else:
                print(f"Unknown command: {cmd}")
                print("Type 'help' for available commands")
                
        except KeyboardInterrupt:
            print("\nGoodbye! 👋")
            break
        except Exception as e:
            print(f"Error: {e}")

# Example usage
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Search and download papers from arXiv')
    parser.add_argument('query', nargs='?', help='Search query')
    parser.add_argument('-n', '--max_results', type=int, default=5, help='Maximum number of results (default: 5)')
    parser.add_argument('-d', '--download', action='store_true', help='Download the first result')
    parser.add_argument('-i', '--interactive', action='store_true', help='Start interactive mode')
    
    args = parser.parse_args()
    
    if args.interactive:
        interactive_mode()
    elif args.query:
        search_and_download(args.query, args.max_results, args.download)
    else:
        # Default behavior - start interactive mode
        interactive_mode()
