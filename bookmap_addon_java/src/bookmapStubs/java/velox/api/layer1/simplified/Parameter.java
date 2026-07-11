package velox.api.layer1.simplified;

import java.lang.annotation.ElementType;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
import java.lang.annotation.Target;

/** Compile-time stub matching Bookmap API 7.6.0.20. */
@Retention(RetentionPolicy.RUNTIME)
@Target(ElementType.FIELD)
public @interface Parameter {
    String name();

    double step() default 0.0d;

    double minimum() default 0.0d;

    double maximum() default 0.0d;

    boolean reloadOnChange() default false;
}
