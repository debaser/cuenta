"""Word list for fixing OCR slips in item names ("PaTmera" -> "Palmera", "ro11" -> "roll").

Conservative on purpose, because a wrong "fix" is worse than a typo the user can edit:
  - only words that are NOT in the list are touched;
  - digits glued to letters are read as the letters they usually are (1 -> l, 0 -> o, 5 -> s ...) and kept only if
    the result is a known word;
  - otherwise only a same-length word that differs by substitutions (cheap for look-alikes such as l/t/i/1, o/0)
    or by one swap of neighbouring letters, never by adding/dropping letters (a printer may cut a name: "BURRAT"),
    and only for words of 5+ letters with exactly one best candidate.
Everything is ASCII and lowercase; accents are ignored when comparing and not restored (tickets rarely print them)."""
import re
import unicodedata

_WORDS = """
cafe cafes solo cortado americano capuchino cappuccino latte espresso ristretto macchiato mocca moka descafeinado leche
desnatada avena soja bombon carajillo irish te tes infusion manzanilla poleo menta tila rooibos chocolate colacao batido
zumo naranja limon pomelo manzana piña pina tomate melocoton albaricoque fresa platano uva sandia melon
agua gas grifo mineral tonica soda sifon refresco refrescos cola coca pepsi fanta sprite aquarius nestea redbull burn
cerveza cervezas cana canas zurito tercio botellin jarra pinta radler shandy lager pilsner ipa tostada negra rubia
vino vinos tinto blanco rosado clarete crianza reserva rioja ribera txakoli albariño albarino verdejo rueda cava champan
champagne sidra vermut vermouth sangria tinto verano calimocho ginebra gin ron whisky whiskey vodka tequila brandy
coñac conac licor orujo pacharan patxaran anis cointreau baileys amaretto limoncello copa copas chupito chupitos
combinado cubata hielo hielos rodaja pintxo pintxos pincho pinchos tapa tapas racion raciones media medias bocadillo
bocadillos bocata sandwich sandwiches hamburguesa hamburguesas perrito completo tostas tostada tostadas torrija torrijas
croissant croissants cruasan cruasanes napolitana napolitanas palmera palmeras magdalena magdalenas bizcocho bizcochos
tarta tartas pastel pasteles brownie brownies cookie cookies galleta galletas donut donuts dona rosquilla rosquillas
churro churros porra porras cinnamon roll rolls bagel bagels muffin muffins cupcake waffle waffles gofre gofres crepe
crepes pancake pancakes helado helados bola bolas sorbete nata flan natillas cuajada arroz leche tiramisu cheesecake
profiteroles coulant fondant mousse trufa trufas bombones turron polvoron mazapan
ensalada ensaladas mixta ensaladilla rusa gilda gildas anchoa anchoas boqueron boquerones sardina sardinas atun bonito
bacalao merluza rape lubina dorada salmon pulpo calamares calamar chipirones chipiron sepia gambas gamba langostino
langostinos mejillones mejillon almejas almeja navajas percebes cigalas cigala bogavante txangurro txipirones
croquetas croqueta bravas patatas patata fritas frita tortilla tortillas huevo huevos revuelto revueltos jamon serrano
iberico chorizo salchichon lomo morcilla panceta bacon beicon txistorra longaniza salchicha salchichas embutido
embutidos queso quesos manchego idiazabal brie camembert cabra roquefort parmesano mozzarella burrata gorgonzola
provolone ricotta pimientos pimiento piquillo padron guindilla guindillas champiñon champiñones champinon champinones
setas seta verduras verdura menestra espinacas acelgas judias alubias garbanzos lentejas guisantes
sopa sopas crema caldo cocido potaje fabada marmitako ajoarriero callos carrillera carrilleras cordero cochinillo
chuleta chuletas chuleton entrecot solomillo ternera cerdo pollo pavo conejo hamburguesa albondigas albondiga costillas
costilla secreto presa pluma lacon
pizza pizzas margarita margherita marinara calzone focaccia pasta spaghetti espaguetis macarrones tallarines lasaña
lasagna ravioli raviolis gnocchi ñoquis carbonara amatriciana bolognesa boloñesa pesto arrabbiata fettuccine penne
tagliatelle risotto paella fideua fideos nachos burrito burritos tacos taco quesadilla quesadillas falafel hummus kebab
durum shawarma wrap wraps sushi nigiri maki sashimi ramen noodles
menu menus postre postres entrante entrantes principal principales plato platos ración guarnicion salsa salsas
extra extras suplemento supplement doble simple grande pequeño pequeña mediano pequeno pequena
desayuno almuerzo comida merienda cena brunch
pan panes barra barras baguette chapata integral centeno semillas mantequilla mermelada miel aceite oliva tomate
ajo cebolla pimienta sal vinagre mostaza ketchup mayonesa alioli
coffee tea milk sugar butter cheese cake toast juice water beer wine salad burger fries chips sauce
""".split()


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").replace("ñ", "n")


WORDS = {_norm(w) for w in _WORDS}
_BY_LEN: dict[int, list[str]] = {}
for _w in WORDS:
    _BY_LEN.setdefault(len(_w), []).append(_w)

# letters an OCR commonly mistakes for each other: substituting them is "cheap"
_LOOKALIKE = [set("l1it|!"), set("o0qd"), set("s5"), set("b8"), set("g9q"), set("z2"), set("rn"), set("u v".replace(" ", "")), set("ceo"), set("mn")]
_DIGIT_TO_LETTER = {"0": "o", "1": "l", "5": "s", "8": "b", "6": "g", "2": "z"}


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return 0.5 if any(a in g and b in g for g in _LOOKALIKE) else 1.0


def _cost(token: str, word: str) -> float:
    """Substitutions only (same length), or one swap of neighbours: the cheapest way to turn token into word."""
    direct = sum(_sub_cost(a, b) for a, b in zip(token, word))
    best = direct
    for i in range(len(token) - 1):                       # one transposition
        if token[i] != token[i + 1] and token[i] == word[i + 1] and token[i + 1] == word[i]:
            rest = sum(_sub_cost(a, b) for k, (a, b) in enumerate(zip(token, word)) if k not in (i, i + 1))
            best = min(best, 1.0 + rest)
    return best


def _shape(original: str, corrected: str) -> str:
    """Give the corrected word the same capitalisation as the token it replaces."""
    letters = [c for c in original if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return corrected.upper()
    if original[:1].isupper():
        return corrected.capitalize()
    return corrected


def fix_word(token: str) -> str:
    if len(token) < 3 or token.isdigit():
        return token
    n = _norm(token)
    if n in WORDS:
        return token
    # a capital I inside a lower-case word is almost always a lower-case l ("roIl" -> "roll")
    fixed_i = re.sub(r"(?<=[a-z])I", "l", token)
    if fixed_i != token and _norm(fixed_i) in WORDS:
        return _shape(token, _norm(fixed_i))
    has_digit = any(c.isdigit() for c in n)
    if has_digit and sum(c.isalpha() for c in n) >= 2:
        letters = "".join(_DIGIT_TO_LETTER.get(c, c) for c in n)          # ro11 -> roll
        if letters in WORDS:
            return _shape(token, letters)
        n = letters
    if len(n) < 5 or not n.isalpha():
        return token
    scored = sorted(((_cost(n, w), w) for w in _BY_LEN.get(len(n), [])), key=lambda x: x[0])
    if scored and scored[0][0] <= 1.0 and (len(scored) == 1 or scored[1][0] > scored[0][0]):
        return _shape(token, scored[0][1])
    return token


_TOKEN = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]+")


def fix_name(text: str) -> str:
    """Fix the words of an item name (numbers, punctuation and spacing stay as they are)."""
    return _TOKEN.sub(lambda m: fix_word(m.group(0)), text)
