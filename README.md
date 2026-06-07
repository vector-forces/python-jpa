# python-jpa

`python-jpa` is a lightweight, Spring Data JPA-inspired Object-Document Mapper (ODM) for MongoDB built on top of **Python 3.13+**, **Pydantic v2**, and **PyMongo**. 

It uses advanced Python metaprogramming (`metaclass`) to dynamically generate MongoDB queries at runtime based on method naming conventions or explicit query declarations, drastically reducing boilerplate code.

---

## 🚀 Features

* **JPA-Like Repository Pattern:** Declare an interface, and let the metaclass handle query assembly dynamically.
* **Query Naming Conventions:** Instantly supports `find_by_*`, `find_all_by_*`, `exists_by_*`, and `count_by_*` pattern resolutions.
* **Custom Query Annotations:** Bind complex MongoDB query templates with custom parameter injections using the `@query` decorator.
* **Data Validation:** Fully powered by Pydantic v2 for robust runtime type checking and parsing.
* **Zero-Boilerplate ID Mapping:** Automatically handles conversions between stringified hexadecimal keys and native BSON `ObjectId` footprints.
* **Environment Profiles:** Flexible configurations handling multi-stage deployment environments (`dev`, `prod`, etc.) via YAML interpolation.

---

## 📁 Directory Structure

```text
.
├── README.md
├── jpa
│   ├── __init__.py
│   ├── config.py         # App bootstrapping, YAML parsing & environment mapping
│   └── mongo
│       ├── __init__.py
│       └── interface.py  # Repository core interface, custom metadata, and queries
├── pyproject.toml
└── requirements.txt

🛠️ Getting Started
Prerequisites
• Python 3.13 or higher
Installation & Virtual Env Setup
1. Clone the repository and navigate to its root: cd python-jpa
2. Spin up a virtual environment and update your pip core dependencies: python3 -m venv venv source venv/bin/activate  # On Windows use: venv\Scripts\activate pip install --upgrade pip
3. Install the project library dependencies: pip install -r requirements.txt
⚙️ Configuration Management
The framework supports multi-profile YAML configurations with real-time environment substitution syntax (e.g., ${ENV_VAR:default_value}).
Create a resources/ directory at the root of your execution workspace and include your application sheets:
1. Master Configuration (resources/settings.yml)
app:
  profile: ${APP_PROFILE:dev} # Swaps profile to target environment configuration

2. Environment Profile Configuration (resources/settings-dev.yml)
mongodb:
  uri: ${MONGO_URI:mongodb://localhost:27017}
  database: ${MONGO_DB_NAME:jpa_database}

framework:
  logging:
    enabled: true
    level: "DEBUG"

📖 Usage Example
Here is a quick overview of how you can configure a domain entity and generate auto-implemented interface pipelines:
1. Define your Document Model
from pydantic import Field
from jpa.mongo.interface import DocumentModel, document

@document(name="users")
class User(DocumentModel):
    id: str = Field(alias="_id", default=None)
    username: str
    email: str
    age: int

2. Declare your Interface Repository
By extending IRepository[T], method naming patterns are captured and converted into database interactions seamlessly.
from jpa.mongo.interface import IRepository, query
from typing import List, Optional

class UserRepository(IRepository[User]):
    
    # 1. Query generation by structural method naming convention
    def find_by_username(self, username: str) -> Optional[User]: ...
    
    def count_by_age(self, age: int) -> int: ...

    # 2. Templated declaration using placeholder substitutions
    @query(definition={"email": "?0", "age": {"$gte": "?1"}})
    def find_by_email_and_min_age(self, email: str, min_age: int) -> List[User]: ...

3. Execute CRUD Actions
from jpa.config import close_db_connection

# Initialize repository instance
user_repo = UserRepository()

# Save a document
new_user = User(username="pratyush", email="pratyush@example.com", age=25)
user_id = user_repo.save(new_user)

# Fetch using automatic query generation
user = user_repo.find_by_username("pratyush")
print(f"Found User: {user.email if user else 'Not Found'}")

# Clean up connections on process termination
close_db_connection()

📜 License
This project is open-source software licensed under the MIT License.