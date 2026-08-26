from dotenv import load_dotenv

from stdio_utf8 import configure_utf8_stdio

configure_utf8_stdio()
load_dotenv()

from coordinator import run_deep_research


def main():
    user_query = input("Enter your research query: ")
    result = run_deep_research(user_query, interactive=True)
    with open("research_result.md", "w", encoding="utf-8") as f:
        f.write(result)

    print("Research result saved to research_result.md")


if __name__ == "__main__":
    main()
