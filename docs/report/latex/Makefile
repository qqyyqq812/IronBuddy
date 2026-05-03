TEX = main
LATEX = xelatex -interaction=nonstopmode -halt-on-error

.PHONY: all clean distclean view

all: $(TEX).pdf

$(TEX).pdf: $(TEX).tex sections/*.tex
	$(LATEX) $(TEX).tex
	$(LATEX) $(TEX).tex

view: $(TEX).pdf
	@xdg-open $(TEX).pdf 2>/dev/null || evince $(TEX).pdf 2>/dev/null || zathura $(TEX).pdf 2>/dev/null || echo "Open $(TEX).pdf manually"

clean:
	rm -f *.aux *.log *.out *.toc *.bbl *.blg *.fdb_latexmk *.fls *.synctex.gz sections/*.aux

distclean: clean
	rm -f $(TEX).pdf
