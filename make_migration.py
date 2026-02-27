import sys
from alembic.config import main

if __name__ == '__main__':
    print("Upgrading database...")
    sys.argv = ["alembic", "upgrade", "head"]
    try:
        main()
    except SystemExit:
        pass
    
    print("Creating revision...")
    sys.argv = ["alembic", "revision", "--autogenerate", "-m", "add_music_file_to_content_plan"]
    main()
