# dealsteal

Watches Trade Me for the things on your wishlist and pushes the good finds to
your phone. Every hour it searches each wishlist item, ignores listings it has
already seen, applies hard rules for price, region and seller type, then asks
an LLM to judge what is left against your own written criteria. Only genuine
deals get sent, as a single ntfy notification with the price, a reason, any red
flags and a suggested maximum offer. It runs in GitHub Actions and remembers
what it has seen in a SQLite database stored on a `state` branch.

Setup, configuration and design notes: [docs/SETUP.md](docs/SETUP.md)
