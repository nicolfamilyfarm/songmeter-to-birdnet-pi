.PHONY: man clean-man

man:
	mkdir -p man
	doxygen Doxyfile

clean-man:
	rm -rf man
