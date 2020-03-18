import rcssmin
import sass

# Map SCSS source files to CSS destination files
sass_map = {"static/scss/style.scss": "static/css/style.css"}
# Map un-minified CSS source files to minified CSS destination
css_map = {"static/css/style.css": "static/css/style.min.css"}


def compile_sass_to_css(sass_map):
    print("Compiling SCSS to CSS:")
    for source, dest in sass_map.items():
        with open(dest, "w") as outfile:
            outfile.write(sass.compile(filename=source))
        print(f"{source} compiled to {dest}")


def minify_css(css_map):
    print("Minify CSS files:")
    for source, dest in css_map.items():
        with open(source, "r") as infile:
            with open(dest, "w") as outfile:
                outfile.write(rcssmin.cssmin(infile.read()))
        print(f"{source} minified to {dest}")


if __name__ == "__main__":
    print()
    print("Starting runner")
    print("--------------------")
    compile_sass_to_css(sass_map)
    print("--------------------")
    minify_css(css_map)
    print("--------------------")
    print("Done")
    print()