# fastapi 文档（Context7 抓取，评估语料）


## Topic: dependency injection

### fastapi.Depends

Source: https://fastapi.tiangolo.com/reference/dependencies

Declares a FastAPI dependency using a dependable callable. Manages dependency execution, caching across a single request, and lifecycle scope for dependencies with yield.

```APIDOC
## fastapi.Depends

### Description
Declare a FastAPI dependency. It takes a single "dependable" callable (like a function). FastAPI calls the dependency during request handling.

### Signature
```python
def Depends(
    dependency: Callable[..., Any] | None = None,
    *,
    use_cache: bool = True,
    scope: Literal["function", "request"] | None = None
) -> Any
```

### Parameters
- **dependency** (`Callable[..., Any] | None`, optional, default: `None`) - A dependable callable (such as a function). FastAPI will execute it automatically.
- **use_cache** (`bool`, optional, default: `True`) - When `True`, the result of the dependency is cached and re-used if the dependency is encountered multiple times within the same request. Set to `False` to force re-execution.
- **scope** (`Literal['function', 'request'] | None`, optional, default: `None`) - Defines execution scope for dependencies using `yield`. `"function"` executes around the path operation function (exits before response is sent). `"request"` executes around the entire request-response cycle (exits after response is sent).

### Returns
- `Any` - Returns a dependency marker instance (`params.Depends`) recognized by FastAPI parameter injection.

### Usage Example
```python
from typing import Annotated
from fastapi import Depends, FastAPI

app = FastAPI()

async def common_parameters(q: str | None = None, skip: int = 0, limit: int = 100):
    return {"q": q, "skip": skip, "limit": limit}

@app.get("/items/")
async def read_items(commons: Annotated[dict, Depends(common_parameters)]):
    return commons
```
```

--------------------------------

### Define and use a dependency with FastAPI

Source: https://fastapi.tiangolo.com/tutorial/dependencies

This snippet demonstrates how to define a reusable dependency function and inject it into path operation functions using `Depends`. The dependency function `common_parameters` extracts query parameters and returns them as a dictionary.

```python
from typing import Annotated

from fastapi import Depends, FastAPI

app = FastAPI()


async def common_parameters(q: str | None = None, skip: int = 0, limit: int = 100):
    return {"q": q, "skip": skip, "limit": limit}


@app.get("/items/")
async def read_items(commons: Annotated[dict, Depends(common_parameters)]):
    return commons


@app.get("/users/")
async def read_users(commons: Annotated[dict, Depends(common_parameters)]):
    return commons
```

```python
from fastapi import Depends, FastAPI

app = FastAPI()


async def common_parameters(q: str | None = None, skip: int = 0, limit: int = 100):
    return {"q": q, "skip": skip, "limit": limit}


@app.get("/items/")
async def read_items(commons: dict = Depends(common_parameters)):
    return commons


@app.get("/users/")
async def read_users(commons: dict = Depends(common_parameters)):
    return commons
```

--------------------------------

### Define get_current_user dependency in FastAPI

Source: https://fastapi.tiangolo.com/tutorial/security/get-current-user

Uses OAuth2PasswordBearer as a sub-dependency to retrieve and decode the user token. Inject get_current_user into path operations to authenticate requests.

```python
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

app = FastAPI()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool | None = None


def fake_decode_token(token):
    return User(
        username=token + "fakedecoded", email="john@example.com", full_name="John Doe"
    )


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    user = fake_decode_token(token)
    return user


@app.get("/users/me")
async def read_users_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user

```

```python
from fastapi import Depends, FastAPI
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

app = FastAPI()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool | None = None


def fake_decode_token(token):
    return User(
        username=token + "fakedecoded", email="john@example.com", full_name="John Doe"
    )


async def get_current_user(token: str = Depends(oauth2_scheme)):
    user = fake_decode_token(token)
    return user


@app.get("/users/me")
async def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user

```

--------------------------------

### Define SQLModel models and CRUD endpoints in FastAPI

Source: https://fastapi.tiangolo.com/tutorial/sql-databases

Full application example implementing CRUD endpoints for SQLModel entities with SQLite and dependency injection for sessions. Note that using Annotated types for dependencies is preferred in modern FastAPI.

```python
from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Field, Session, SQLModel, create_engine, select


class HeroBase(SQLModel):
    name: str = Field(index=True)
    age: int | None = Field(default=None, index=True)


class Hero(HeroBase, table=True):
    id: int | None = Field(default=None, primary_key=True)
    secret_name: str


class HeroPublic(HeroBase):
    id: int


class HeroCreate(HeroBase):
    secret_name: str


class HeroUpdate(HeroBase):
    name: str | None = None
    age: int | None = None
    secret_name: str | None = None


sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


app = FastAPI()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()


@app.post("/heroes/", response_model=HeroPublic)
def create_hero(hero: HeroCreate, session: Session = Depends(get_session)):
    db_hero = Hero.model_validate(hero)
    session.add(db_hero)
    session.commit()
    session.refresh(db_hero)
    return db_hero


@app.get("/heroes/", response_model=list[HeroPublic])
def read_heroes(
    session: Session = Depends(get_session),
    offset: int = 0,
    limit: int = Query(default=100, le=100),
):
    heroes = session.exec(select(Hero).offset(offset).limit(limit)).all()
    return heroes


@app.get("/heroes/{hero_id}", response_model=HeroPublic)
def read_hero(hero_id: int, session: Session = Depends(get_session)):
    hero = session.get(Hero, hero_id)
    if not hero:
        raise HTTPException(status_code=404, detail="Hero not found")
    return hero


@app.patch("/heroes/{hero_id}", response_model=HeroPublic)
def update_hero(
    hero_id: int, hero: HeroUpdate, session: Session = Depends(get_session)
):
    hero_db = session.get(Hero, hero_id)
    if not hero_db:
        raise HTTPException(status_code=404, detail="Hero not found")
    hero_data = hero.model_dump(exclude_unset=True)
    hero_db.sqlmodel_update(hero_data)
    session.add(hero_db)
    session.commit()
    session.refresh(hero_db)
    return hero_db


@app.delete("/heroes/{hero_id}")
def delete_hero(hero_id: int, session: Session = Depends(get_session)):
    hero = session.get(Hero, hero_id)
    if not hero:
        raise HTTPException(status_code=404, detail="Hero not found")
    session.delete(hero)
    session.commit()
    return {"ok": True}

```

### Dependencies > What is Dependency Injection

Source: https://fastapi.tiangolo.com/tutorial/dependencies

Dependency Injection in FastAPI allows path operation functions to declare things they require to work, and FastAPI automatically provides those dependencies. This is useful for sharing logic, managing database connections, enforcing security and authentication, and reducing code repetition.

## Topic: pydantic models

### Define deeply nested Pydantic models in FastAPI

Source: https://fastapi.tiangolo.com/tutorial/body-nested-models

Nest Pydantic models within other models and use container types such as lists and sets. FastAPI validates each level and converts the nested structures automatically.

```python
from fastapi import FastAPI
from pydantic import BaseModel, HttpUrl

app = FastAPI()


class Image(BaseModel):
    url: HttpUrl
    name: str


class Item(BaseModel):
    name: str
    description: str | None = None
    price: float
    tax: float | None = None
    tags: set[str] = set()
    images: list[Image] | None = None


class Offer(BaseModel):
    name: str
    description: str | None = None
    price: float
    items: list[Item]


@app.post("/offers/")
async def create_offer(offer: Offer):
    return offer

```

--------------------------------

### Instantiate Pydantic Models

Source: https://fastapi.tiangolo.com/features

Create model instances using direct arguments or dictionary unpacking.

```python
my_user: User = User(id=3, name="John Doe", joined="2018-07-19")

second_user_data = {
    "id": 4,
    "name": "Mary",
    "joined": "2018-11-30",
}

my_second_user: User = User(**second_user_data)
```

--------------------------------

### Define Python Types and Pydantic Models

Source: https://fastapi.tiangolo.com/features

Use standard Python type hints and Pydantic models to define data structures and enable editor autocompletion.

```python
from datetime import date

from pydantic import BaseModel

# Declare a variable as a str
# and get editor support inside the function
def main(user_id: str):
    return user_id


# A Pydantic model
class User(BaseModel):
    id: int
    name: str
    joined: date
```

--------------------------------

### Define and parse data with Pydantic BaseModel in Python

Source: https://fastapi.tiangolo.com/python-types

Instantiating a Pydantic model automatically validates types and converts compatible raw values (e.g., parsing strings into integers or datetime objects).

```python
from datetime import datetime

from pydantic import BaseModel


class User(BaseModel):
    id: int
    name: str = "John Doe"
    signup_ts: datetime | None = None
    friends: list[int] = []


external_data = {
    "id": "123",
    "signup_ts": "2017-06-01 12:22",
    "friends": [1, "2", b"3"],
}
user = User(**external_data)
print(user)
# > User id=123 name='John Doe' signup_ts=datetime.datetime(2017, 6, 1, 12, 22) friends=[1, 2, 3]
print(user.id)
# > 123

```

--------------------------------

### Access Pydantic model attributes in a path operation

Source: https://fastapi.tiangolo.com/tutorial/body

Access model fields directly as object attributes or convert the model to a dictionary using item.model_dump().

```python
from fastapi import FastAPI
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    description: str | None = None
    price: float
    tax: float | None = None


app = FastAPI()


@app.post("/items/")
async def create_item(item: Item):
    item_dict = item.model_dump()
    if item.tax is not None:
        price_with_tax = item.price + item.tax
        item_dict.update({"price_with_tax": price_with_tax})
    return item_dict

```

## Topic: middleware

### Create HTTP middleware in FastAPI

Source: https://fastapi.tiangolo.com/tutorial/middleware

Uses `@app.middleware("http")` to intercept incoming requests and modify outgoing responses. The `call_next` function forwards the request to the corresponding path operation.

```python
import time

from fastapi import FastAPI, Request

app = FastAPI()


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = time.perf_counter() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response

```

--------------------------------

### Add an ASGI middleware using FastAPI's add_middleware

Source: https://fastapi.tiangolo.com/advanced/middleware

Recommended method to register middleware in FastAPI to ensure internal error handling and custom exception handlers function correctly.

```python
from fastapi import FastAPI
from unicorn import UnicornMiddleware

app = FastAPI()

app.add_middleware(UnicornMiddleware, some_config="rainbow")

```

--------------------------------

### CORSMiddleware

Source: https://fastapi.tiangolo.com/reference/middleware

Middleware for configuring Cross-Origin Resource Sharing (CORS) support in FastAPI applications.

```APIDOC
## CORSMiddleware

### Parameters and Members
- **app**
- **allow_origins**
- **allow_methods**
- **allow_headers**
- **allow_all_origins**
- **allow_all_headers**
- **allow_credentials**
- **preflight_explicit_allow_origin**
- **allow_origin_regex**
- **allow_private_network**
- **simple_headers**
- **preflight_headers**
- **is_allowed_origin**
- **preflight_response**
- **simple_response**
- **send**
- **allow_explicit_origin**
```

--------------------------------

### fastapi.middleware.httpsredirect.HTTPSRedirectMiddleware

Source: https://fastapi.tiangolo.com/reference/middleware

Middleware that redirects all incoming HTTP requests to HTTPS. Can be imported directly from fastapi.middleware.httpsredirect.

```APIDOC
## fastapi.middleware.httpsredirect.HTTPSRedirectMiddleware

### Description
Middleware that enforces HTTPS by redirecting all incoming HTTP requests to their HTTPS equivalent.

### Import
```python
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
```

### Constructor Signature
```python
HTTPSRedirectMiddleware(app: ASGIApp) -> None
```

### Parameters
- **app** (`ASGIApp`) - Required - The ASGI application instance to wrap.

### Instance Attributes
- **app** (`ASGIApp`) - The wrapped ASGI application.
```

### Middleware

Source: https://fastapi.tiangolo.com/tutorial/middleware

A middleware in FastAPI is a function that processes every request before it reaches a path operation, and every response before it is returned. It can inspect or modify incoming requests, forward them to the application via a call_next function, and inspect or modify the generated response. Dependencies with yield have their exit code executed after middleware, and background tasks also run after all middleware has finished.

## Topic: background tasks

### Define and schedule a background task in FastAPI

Source: https://fastapi.tiangolo.com/tutorial/background-tasks

Declare a `BackgroundTasks` parameter in the path operation function and register tasks with `.add_task()`. The scheduled task will execute after the response is returned to the client.

```python
from fastapi import BackgroundTasks, FastAPI

app = FastAPI()


def write_notification(email: str, message=""):
    with open("log.txt", mode="w") as email_file:
        content = f"notification for {email}: {message}"
        email_file.write(content)


@app.post("/send-notification/{email}")
async def send_notification(email: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(write_notification, email, message="some notification")
    return {"message": "Notification sent in the background"}

```

--------------------------------

### Use BackgroundTasks with Dependency Injection in FastAPI

Source: https://fastapi.tiangolo.com/tutorial/background-tasks

Declare BackgroundTasks in both path operations and dependencies to accumulate tasks executed after returning a response. FastAPI automatically reuses the same instance across the request lifecycle.

```python
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI

app = FastAPI()


def write_log(message: str):
    with open("log.txt", mode="a") as log:
        log.write(message)


def get_query(background_tasks: BackgroundTasks, q: str | None = None):
    if q:
        message = f"found query: {q}\n"
        background_tasks.add_task(write_log, message)
    return q


@app.post("/send-notification/{email}")
async def send_notification(
    email: str, background_tasks: BackgroundTasks, q: Annotated[str, Depends(get_query)]
):
    message = f"message to {email}\n"
    background_tasks.add_task(write_log, message)
    return {"message": "Message sent"}

```

```python
from fastapi import BackgroundTasks, Depends, FastAPI

app = FastAPI()


def write_log(message: str):
    with open("log.txt", mode="a") as log:
        log.write(message)


def get_query(background_tasks: BackgroundTasks, q: str | None = None):
    if q:
        message = f"found query: {q}\n"
        background_tasks.add_task(write_log, message)
    return q


@app.post("/send-notification/{email}")
async def send_notification(
    email: str, background_tasks: BackgroundTasks, q: str = Depends(get_query)
):
    message = f"message to {email}\n"
    background_tasks.add_task(write_log, message)
    return {"message": "Message sent"}

```

### Background Tasks > Add the background task

Source: https://fastapi.tiangolo.com/tutorial/background-tasks

Background tasks are registered inside a path operation function by calling the add_task method on a BackgroundTasks instance. The method accepts the target task function along with any positional and keyword arguments that should be passed to it during execution.

--------------------------------

### Background Tasks > Create a task function

Source: https://fastapi.tiangolo.com/tutorial/background-tasks

A background task is a standard function that can receive parameters and be defined using either async def or standard def. FastAPI automatically handles execution appropriately depending on how the function is defined.

--------------------------------

### Background Tasks and Dependencies with yield, Technical Details

Source: https://fastapi.tiangolo.com/advanced/advanced-dependencies

In FastAPI 0.106.0, the execution of exit code in dependencies with yield was changed to run before background tasks finish, preventing resources from being held unnecessarily while responses travel through the network. Background tasks should be treated as independent logic and manage their own resources, such as creating their own database sessions. Instead of passing yielded objects or database sessions directly to a background task, pass identifiers and retrieve the necessary objects inside the background task itself.

## Topic: async await

### Define an asynchronous function with async def in Python

Source: https://fastapi.tiangolo.com/async

Declares a coroutine function that supports asynchronicity and allows the use of await expressions inside it.

```python
async def get_burgers(number: int):
    # Do some asynchronous stuff to create the burgers
    return burgers

```

--------------------------------

### Await an asynchronous function call in Python

Source: https://fastapi.tiangolo.com/async

Pauses execution until the asynchronous operation completes, allowing Python to perform other tasks in the meantime.

```python
burgers = await get_burgers(2)

```

--------------------------------

### Stream async generator responses using StreamingResponse in FastAPI

Source: https://fastapi.tiangolo.com/advanced/custom-response

Stream data using an async generator wrapped in `StreamingResponse`. Ensure async generators include an `await` statement (such as `anyio.sleep(0)`) so the event loop can process task cancellation.

```python
import anyio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()


async def fake_video_streamer():
    for i in range(10):
        yield b"some fake video bytes"
        await anyio.sleep(0)


@app.get("/")
async def main():
    return StreamingResponse(fake_video_streamer())

```

### async and await

Source: https://fastapi.tiangolo.com/async

Modern Python allows defining asynchronous code using the async def and await keywords. The await keyword indicates an operation that requires waiting, allowing Python to pause execution of that function and perform other tasks, such as handling incoming requests, before returning with the results.

--------------------------------

### async and await > More technical details

Source: https://fastapi.tiangolo.com/async

Functions defined with async def must be awaited and can only be called inside other async functions. When using FastAPI, developers do not need to manage the initial async entry point manually, as FastAPI automatically manages the execution of path operation functions.

## Topic: automatic api docs

### Configure custom docs and redoc URLs in FastAPI

Source: https://fastapi.tiangolo.com/reference/fastapi

Sets custom endpoints or disables interactive API documentation interfaces in FastAPI.

```python
app = FastAPI(docs_url="/documentation", redoc_url=None)

```

```python
from fastapi import FastAPI

app = FastAPI(docs_url="/documentation", redoc_url="redocumentation")

```

--------------------------------

### Disable automatic docs and serve custom Swagger UI and ReDoc

Source: https://fastapi.tiangolo.com/how-to/custom-docs-ui-assets

Disable default docs by setting docs_url and redoc_url to None, then create custom path operations that serve Swagger UI and ReDoc with custom CDN URLs. Include the OAuth2 redirect helper for authentication support.

```python
from fastapi import FastAPI
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)

app = FastAPI(docs_url=None, redoc_url=None)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=app.title + " - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js",
        swagger_css_url="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css",
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect():
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html():
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=app.title + " - ReDoc",
        redoc_js_url="https://unpkg.com/redoc@2/bundles/redoc.standalone.js",
    )


@app.get("/users/{username}")
async def read_user(username: str):
    return {"message": f"Hello {username}"}
```

### Example > Interactive API docs

Source: https://fastapi.tiangolo.com

FastAPI provides built-in automatic API documentation. It generates interactive documentation powered by Swagger UI available at `/docs`, as well as alternative documentation powered by ReDoc available at `/redoc`.

--------------------------------

### FastAPI features > Automatic docs

Source: https://fastapi.tiangolo.com/features

The framework provides interactive API documentation out of the box, including Swagger UI for direct browser-based testing and ReDoc for alternative documentation views.

--------------------------------

### FastAPI Reference > Documentation URLs

Source: https://fastapi.tiangolo.com/reference/fastapi

FastAPI provides automatic interactive API documentation by default using Swagger UI at `/docs` and ReDoc at `/redoc`. Both documentation endpoints can be customized to custom paths or disabled by setting them to `None`, and both are automatically disabled if `openapi_url` is disabled.